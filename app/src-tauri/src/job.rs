//! Windows Job Object holding the engine's whole process tree.
//!
//! The release sidecar is a PyInstaller one-file launcher that runs Python as a
//! child process, and `uv run` does the same in development. Killing only the
//! direct child can leave Python running. Everything in the job is killed when
//! the job is terminated or its last handle closes (including if the app
//! itself crashes).

#[cfg(windows)]
mod imp {
    use std::mem::{size_of, zeroed};
    use std::ptr::null;

    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    pub struct ProcessTree(HANDLE);

    // The handle is an owned kernel object; it can move between threads.
    unsafe impl Send for ProcessTree {}
    unsafe impl Sync for ProcessTree {}

    impl ProcessTree {
        /// Put a freshly spawned process (and every process it starts later) in a new job.
        pub fn adopt(child: &tokio::process::Child) -> Option<Self> {
            let process = child.raw_handle()? as HANDLE;
            unsafe {
                let job = CreateJobObjectW(null(), null());
                if job.is_null() {
                    return None;
                }
                let tree = ProcessTree(job);
                let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = zeroed();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                let ok = SetInformationJobObject(
                    job,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const core::ffi::c_void,
                    size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                ) != 0
                    && AssignProcessToJobObject(job, process) != 0;
                ok.then_some(tree)
            }
        }

        pub fn kill(&self) {
            unsafe {
                TerminateJobObject(self.0, 1);
            }
        }
    }

    impl Drop for ProcessTree {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}

#[cfg(not(windows))]
mod imp {
    /// No-op off Windows: the shell only ships on Windows.
    pub struct ProcessTree;

    impl ProcessTree {
        pub fn adopt(_child: &tokio::process::Child) -> Option<Self> {
            None
        }

        pub fn kill(&self) {}
    }
}

pub use imp::ProcessTree;
