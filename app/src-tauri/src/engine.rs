//! Owns the Python engine sidecar and speaks its line-delimited JSON-RPC
//! (engine/src/dieline_engine/rpc.py) over stdin/stdout.
//!
//! Requests from the webview arrive through the `engine_request` command;
//! engine notifications are re-emitted to the webview as `engine-event`.
//! If the engine exits, pending requests fail and the next request restarts it.

use std::collections::HashMap;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use serde::Serialize;
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, State};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::{oneshot, Mutex};

const EVENT: &str = "engine-event";
const REQUEST_TIMEOUT: Duration = Duration::from_secs(60);

#[derive(Debug, Clone, Serialize)]
pub struct EngineError {
    pub code: i64,
    pub message: String,
}

impl EngineError {
    fn new(message: impl Into<String>) -> Self {
        Self { code: -32000, message: message.into() }
    }
}

type Reply = Result<Value, EngineError>;
type Pending = Arc<Mutex<HashMap<u64, oneshot::Sender<Reply>>>>;

struct Running {
    stdin: ChildStdin,
    generation: u64,
}

pub struct Engine {
    running: Arc<Mutex<Option<Running>>>,
    pending: Pending,
    next_id: AtomicU64,
    generation: AtomicU64,
}

impl Engine {
    pub fn new() -> Self {
        Self {
            running: Arc::new(Mutex::new(None)),
            pending: Arc::new(Mutex::new(HashMap::new())),
            next_id: AtomicU64::new(1),
            generation: AtomicU64::new(0),
        }
    }

    /// Spawn the engine if it isn't running.
    pub async fn ensure_started(&self, app: &AppHandle) -> Result<(), EngineError> {
        let mut running = self.running.lock().await;
        if running.is_some() {
            return Ok(());
        }
        let (program, args, cwd) = engine_command()?;
        let mut cmd = Command::new(&program);
        cmd.args(&args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        if let Some(dir) = cwd {
            cmd.current_dir(dir);
        }
        #[cfg(windows)]
        {
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        let mut child = cmd
            .spawn()
            .map_err(|e| EngineError::new(format!("could not start engine `{}`: {e}", program.display())))?;

        let stdin = child.stdin.take().expect("piped stdin");
        let stdout = child.stdout.take().expect("piped stdout");
        let stderr = child.stderr.take().expect("piped stderr");
        let generation = self.generation.fetch_add(1, Ordering::SeqCst) + 1;
        *running = Some(Running { stdin, generation });

        // stderr: engine logs.
        tauri::async_runtime::spawn(async move {
            let mut lines = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                eprintln!("[engine] {line}");
            }
        });

        // stdout: replies and notifications.
        let app = app.clone();
        let pending = self.pending.clone();
        let running_slot = self.running.clone();
        tauri::async_runtime::spawn(async move {
            let mut lines = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                route(&app, &pending, &line).await;
            }
            let code = wait_exit(child).await;
            {
                let mut slot = running_slot.lock().await;
                if slot.as_ref().map(|r| r.generation) == Some(generation) {
                    *slot = None;
                }
            }
            for (_, tx) in pending.lock().await.drain() {
                let _ = tx.send(Err(EngineError::new("engine exited")));
            }
            let _ = app.emit(EVENT, json!({ "type": "engine.exit", "code": code }));
        });
        Ok(())
    }

    pub async fn request(&self, app: &AppHandle, method: String, params: Value) -> Reply {
        self.ensure_started(app).await?;
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().await.insert(id, tx);

        let mut line = serde_json::to_string(&json!({
            "jsonrpc": "2.0", "id": id, "method": method, "params": params,
        }))
        .map_err(|e| EngineError::new(e.to_string()))?;
        line.push('\n');

        let write = async {
            let mut running = self.running.lock().await;
            let r = running.as_mut().ok_or_else(|| EngineError::new("engine is not running"))?;
            r.stdin.write_all(line.as_bytes()).await?;
            r.stdin.flush().await
        };
        if let Err(err) = write.await.map_err(|e: std::io::Error| EngineError::new(e.to_string())) {
            self.pending.lock().await.remove(&id);
            return Err(err);
        }

        match tokio::time::timeout(REQUEST_TIMEOUT, rx).await {
            Ok(Ok(reply)) => reply,
            Ok(Err(_)) => Err(EngineError::new("engine dropped the request")),
            Err(_) => {
                self.pending.lock().await.remove(&id);
                Err(EngineError::new(format!("engine did not answer `{method}` in time")))
            }
        }
    }
}

impl From<EngineError> for std::io::Error {
    fn from(e: EngineError) -> Self {
        std::io::Error::other(e.message)
    }
}

async fn route(app: &AppHandle, pending: &Pending, line: &str) {
    let Ok(msg) = serde_json::from_str::<Value>(line) else {
        eprintln!("[engine] non-JSON stdout: {line}");
        return;
    };
    if let Some(id) = msg.get("id").and_then(Value::as_u64) {
        if let Some(tx) = pending.lock().await.remove(&id) {
            let reply = match msg.get("error") {
                Some(err) => Err(EngineError {
                    code: err.get("code").and_then(Value::as_i64).unwrap_or(-32000),
                    message: err.get("message").and_then(Value::as_str).unwrap_or("engine error").to_string(),
                }),
                None => Ok(msg.get("result").cloned().unwrap_or(Value::Null)),
            };
            let _ = tx.send(reply);
        }
    } else if msg.get("method").and_then(Value::as_str) == Some("event") {
        let _ = app.emit(EVENT, msg.get("params").cloned().unwrap_or(Value::Null));
    }
}

async fn wait_exit(mut child: Child) -> Option<i32> {
    child.wait().await.ok().and_then(|s| s.code())
}

/// Which engine to run:
/// 1. `DIELINE_ENGINE_CMD`, a JSON array such as `["C:/venv/Scripts/python.exe","-m","dieline_engine"]`;
/// 2. release builds: the bundled sidecar next to the app executable;
/// 3. debug builds: `uv run` against the repo's engine project.
fn engine_command() -> Result<(PathBuf, Vec<String>, Option<PathBuf>), EngineError> {
    if let Ok(raw) = std::env::var("DIELINE_ENGINE_CMD") {
        let parts: Vec<String> = serde_json::from_str(&raw)
            .map_err(|e| EngineError::new(format!("DIELINE_ENGINE_CMD must be a JSON array of strings: {e}")))?;
        let (program, args) = parts
            .split_first()
            .ok_or_else(|| EngineError::new("DIELINE_ENGINE_CMD is empty"))?;
        return Ok((PathBuf::from(program), args.to_vec(), None));
    }
    if cfg!(debug_assertions) {
        let engine_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../engine");
        let args = vec![
            "run".into(),
            "--project".into(),
            engine_dir.to_string_lossy().into_owned(),
            "dieline-engine".into(),
        ];
        return Ok((PathBuf::from("uv"), args, None));
    }
    let exe = std::env::current_exe().map_err(|e| EngineError::new(e.to_string()))?;
    let dir = exe.parent().ok_or_else(|| EngineError::new("no executable directory"))?;
    let name = if cfg!(windows) { "dieline-engine.exe" } else { "dieline-engine" };
    Ok((dir.join(name), Vec::new(), Some(dir.to_path_buf())))
}

#[tauri::command]
pub async fn engine_request(
    app: AppHandle,
    engine: State<'_, Engine>,
    method: String,
    params: Option<Value>,
) -> Result<Value, EngineError> {
    engine.request(&app, method, params.unwrap_or_else(|| json!({}))).await
}
