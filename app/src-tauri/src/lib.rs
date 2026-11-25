mod engine;

use tauri::{Manager, RunEvent};

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .manage(engine::Engine::new())
        .setup(|app| {
            // Start the engine early so the first request doesn't wait for Python.
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                if let Err(err) = handle.state::<engine::Engine>().ensure_started(&handle).await {
                    eprintln!("[engine] failed to start: {}", err.message);
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![engine::engine_request])
        .build(tauri::generate_context!())
        .expect("error while building Dieline Studio");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            tauri::async_runtime::block_on(handle.state::<engine::Engine>().shutdown());
        }
    });
}
