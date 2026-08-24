//! Shared low-overhead profiling helpers.

#[cfg(feature = "profile")]
pub(crate) fn thread_cpu_seconds() -> f64 {
    let mut value = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: `value` points to writable storage for the duration of the call.
    let result = unsafe { libc::clock_gettime(libc::CLOCK_THREAD_CPUTIME_ID, &mut value) };
    if result == 0 {
        value.tv_sec as f64 + value.tv_nsec as f64 / 1_000_000_000.0
    } else {
        0.0
    }
}
