"""Child processes never outlive the hub.

* Windows: every child is put in a Job Object with KILL_ON_JOB_CLOSE, so when the hub
  process dies for any reason (Ctrl+C, crash, killed from Task Manager) Windows kills
  Qdrant, the gateway and every edge node with it.
* Everywhere: Python children also watch the hub's PID and exit when it disappears.
"""
import os
import sys
import threading
import time

_job = None


def _win_job():
    global _job
    if _job is not None:
        return _job
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",
                                                      "OtherOperationCount", "ReadTransferCount",
                                                      "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = k32.CreateJobObjectW(None, None)
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # ExtendedLimitInformation
    _job = (k32, job)
    return _job


def adopt(popen):
    """Tie a child process's life to this process."""
    if os.name != "nt":
        return
    try:
        k32, job = _win_job()
        k32.AssignProcessToJobObject(job, int(popen._handle))
    except Exception as exc:  # never block startup on this
        print(f"[lifeline] could not attach {popen.pid} to job: {exc}", file=sys.stderr)


def _alive(pid):
    if os.name == "nt":
        import ctypes
        k32 = ctypes.WinDLL("kernel32")
        h = k32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            return k32.WaitForSingleObject(h, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def watch_parent():
    """In a child process: exit as soon as the hub (EDGEMIND_PARENT_PID) is gone."""
    pid = int(os.getenv("EDGEMIND_PARENT_PID", "0") or 0)
    if not pid:
        return

    def run():
        while True:
            time.sleep(1.5)
            if not _alive(pid):
                os._exit(0)

    threading.Thread(target=run, daemon=True, name="lifeline").start()
