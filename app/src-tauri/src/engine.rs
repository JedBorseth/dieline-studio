//! Owns the Python engine sidecar and speaks its line-delimited JSON-RPC
//! (engine/src/dieline_engine/rpc.py) over stdin/stdout.
//!
//! Requests from the webview arrive through the `engine_request` command;
//! engine notifications are re-emitted to the webview as `engine-event`.
//! If the engine exits, its pending requests fail and the next request starts
//! a new one. Each engine process has a generation number so cleanup of an old
//! process never touches requests sent to its replacement.
//!
//! Lock order: `running` before `pending`.

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
use tokio::time::timeout;

use crate::job::ProcessTree;

const EVENT: &str = "engine-event";
const REPLY_TIMEOUT: Duration = Duration::from_secs(60);
const WRITE_TIMEOUT: Duration = Duration::from_secs(5);
const EXIT_GRACE: Duration = Duration::from_secs(2);

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
/// request id -> (engine generation, reply channel)
type Pending = Arc<Mutex<HashMap<u64, (u64, oneshot::Sender<Reply>)>>>;

struct Running {
    stdin: ChildStdin,
    child: Child,
    /// The engine's whole process tree (Windows); None if the job couldn't be made.
    tree: Option<ProcessTree>,
    generation: u64,
}

impl Running {
    fn kill(&mut self) {
        if let Some(tree) = &self.tree {
            tree.kill();
        }
        let _ = self.child.start_kill();
    }
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
        if running.is_none() {
            *running = Some(self.spawn(app)?);
        }
        Ok(())
    }

    fn spawn(&self, app: &AppHandle) -> Result<Running, EngineError> {
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

        let tree = ProcessTree::adopt(&child);
        let stdin = child.stdin.take().expect("piped stdin");
        let stdout = child.stdout.take().expect("piped stdout");
        let stderr = child.stderr.take().expect("piped stderr");
        let generation = self.generation.fetch_add(1, Ordering::SeqCst) + 1;

        // stderr: engine logs.
        tauri::async_runtime::spawn(async move {
            let mut lines = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                eprintln!("[engine] {line}");
            }
        });

        // stdout: replies and notifications, then cleanup once the engine is gone.
        let app = app.clone();
        let pending = self.pending.clone();
        let running = self.running.clone();
        tauri::async_runtime::spawn(async move {
            // Read raw lines and decode lossily: one bad byte must not stop the reader.
            let mut reader = BufReader::new(stdout);
            let mut buf = Vec::new();
            loop {
                buf.clear();
                match reader.read_until(b'\n', &mut buf).await {
                    Ok(0) | Err(_) => break,
                    Ok(_) => route(&app, &pending, String::from_utf8_lossy(&buf).trim_end()).await,
                }
            }
            // Holding `running` means no new request can be sent to this
            // generation and no replacement can start until cleanup is done.
            let mut slot = running.lock().await;
            if slot.as_ref().map(|r| r.generation) == Some(generation) {
                let r = slot.take().expect("checked above");
                let code = reap(r).await;
                fail_generation(&pending, generation, "engine exited").await;
                let _ = app.emit(EVENT, json!({ "type": "engine.exit", "code": code }));
            }
            // Otherwise it was already retired (timeout or shutdown), which reported it.
            drop(slot);
        });

        Ok(Running { stdin, child, tree, generation })
    }

    pub async fn request(&self, app: &AppHandle, method: String, params: Value) -> Reply {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let mut line = serde_json::to_string(&json!({
            "jsonrpc": "2.0", "id": id, "method": method, "params": params,
        }))
        .map_err(|e| EngineError::new(e.to_string()))?;
        line.push('\n');

        let (tx, rx) = oneshot::channel();
        {
            let mut running = self.running.lock().await;
            if running.is_none() {
                *running = Some(self.spawn(app)?);
            }
            let r = running.as_mut().expect("engine just started");
            self.pending.lock().await.insert(id, (r.generation, tx));
            let write = async {
                r.stdin.write_all(line.as_bytes()).await?;
                r.stdin.flush().await
            };
            let failure = match timeout(WRITE_TIMEOUT, write).await {
                Ok(Ok(())) => None,
                Ok(Err(e)) => Some(format!("could not send to engine: {e}")),
                Err(_) => Some("engine is not responding; restarting it".to_string()),
            };
            if let Some(message) = failure {
                // Retire this engine now rather than waiting for an EOF that a
                // wedged process tree may never send; the next request starts a new one.
                let mut r = running.take().expect("engine just used");
                let generation = r.generation;
                r.kill();
                fail_generation(&self.pending, generation, "engine was restarted").await;
                let _ = app.emit(EVENT, json!({ "type": "engine.exit", "code": null }));
                tauri::async_runtime::spawn(async move {
                    reap(r).await;
                });
                return Err(EngineError::new(message));
            }
        }

        match timeout(REPLY_TIMEOUT, rx).await {
            Ok(Ok(reply)) => reply,
            Ok(Err(_)) => Err(EngineError::new("engine dropped the request")),
            Err(_) => {
                self.pending.lock().await.remove(&id);
                Err(EngineError::new(format!("engine did not answer `{method}` in time")))
            }
        }
    }

    /// Stop the engine when the app exits: close stdin so it finishes cleanly,
    /// then kill it if it hasn't exited after a short grace period.
    pub async fn shutdown(&self) {
        let mut slot = self.running.lock().await;
        if let Some(r) = slot.take() {
            reap(r).await;
        }
    }
}

/// Close the engine's stdin so it can exit cleanly, wait briefly, then kill
/// the whole process tree. Dropping `Running` also closes the job handle.
async fn reap(r: Running) -> Option<i32> {
    let Running { stdin, mut child, tree, .. } = r;
    drop(stdin);
    let code = match timeout(EXIT_GRACE, child.wait()).await {
        Ok(status) => status.ok().and_then(|s| s.code()),
        Err(_) => {
            if let Some(tree) = &tree {
                tree.kill();
            }
            let _ = child.start_kill();
            child.wait().await.ok().and_then(|s| s.code())
        }
    };
    if let Some(tree) = &tree {
        tree.kill(); // grandchildren (Python under the launcher) go too
    }
    code
}

async fn fail_generation(pending: &Pending, generation: u64, message: &str) {
    let mut map = pending.lock().await;
    let ids: Vec<u64> = map.iter().filter(|(_, (g, _))| *g == generation).map(|(id, _)| *id).collect();
    for id in ids {
        if let Some((_, tx)) = map.remove(&id) {
            let _ = tx.send(Err(EngineError::new(message)));
        }
    }
}

async fn route(app: &AppHandle, pending: &Pending, line: &str) {
    let Ok(msg) = serde_json::from_str::<Value>(line) else {
        eprintln!("[engine] non-JSON stdout: {line}");
        return;
    };
    if let Some(id) = msg.get("id").and_then(Value::as_u64) {
        if let Some((_, tx)) = pending.lock().await.remove(&id) {
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
    let path = dir.join(name);
    if !path.is_file() {
        return Err(EngineError::new(format!(
            "the engine ({}) is missing from this install; build installers with tauri.release.conf.json",
            path.display()
        )));
    }
    Ok((path, Vec::new(), Some(dir.to_path_buf())))
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
