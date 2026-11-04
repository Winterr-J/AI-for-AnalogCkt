"""Windows-only, fail-closed process-tree containment for guarded canaries.

The child is created suspended and assigned to a non-breakaway Job Object
before its primary thread is resumed. Importing this module starts no process.
"""

from __future__ import annotations

import msvcrt
import os
import subprocess
import tempfile
import time
from pathlib import Path


class JobIsolationError(RuntimeError):
    """Containment or termination could not be established and verified."""


def run_in_job(command: list[str], *, cwd: Path, timeout: float,
               environment: dict[str, str]) -> subprocess.CompletedProcess:
    """Run one process tree; return only after the entire Job is empty.

    Requires the already-installed pywin32 package in the selected interpreter.
    An incompatible enclosing Job causes assignment to fail closed; no
    CREATE_BREAKAWAY_FROM_JOB or unmanaged fallback is attempted.
    """
    if os.name != "nt":
        raise JobIsolationError("Windows Job Objects required")
    if not command or not Path(command[0]).is_absolute() or timeout <= 0:
        raise JobIsolationError("absolute executable and positive timeout required")
    try:
        import win32api
        import win32con
        import win32event
        import win32job
        import win32process
    except ImportError as error:
        raise JobIsolationError("selected Python lacks Windows Job Object support") from error

    job = process = thread = None
    contained = verified_empty = False
    deadline = time.monotonic() + timeout
    max_active = 0
    with tempfile.TemporaryFile(mode="w+b") as stdout_file, \
            tempfile.TemporaryFile(mode="w+b") as stderr_file, \
            open(os.devnull, "rb") as stdin_file:
        handles = [msvcrt.get_osfhandle(file.fileno()) for file in
                   (stdin_file, stdout_file, stderr_file)]
        try:
            job = win32job.CreateJobObject(None, "")
            win32api.SetHandleInformation(job, win32con.HANDLE_FLAG_INHERIT, 0)
            if win32api.GetHandleInformation(job) & win32con.HANDLE_FLAG_INHERIT:
                raise JobIsolationError("Job handle is inheritable")
            limits = win32job.QueryInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] = (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
            win32job.SetInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation, limits)
            startup = win32process.STARTUPINFO()
            startup.dwFlags = win32process.STARTF_USESTDHANDLES
            startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
            marked = []
            try:
                for handle in handles:
                    os.set_handle_inheritable(handle, True)
                    marked.append(handle)
                process, thread, pid, _ = win32process.CreateProcess(
                    command[0], subprocess.list2cmdline(command), None, None, True,
                    win32process.CREATE_SUSPENDED | win32process.CREATE_NO_WINDOW,
                    environment, str(cwd), startup)
            finally:
                for handle in marked:
                    os.set_handle_inheritable(handle, False)
            # No user code can run before this assignment succeeds.
            win32job.AssignProcessToJobObject(job, process)
            if not win32job.IsProcessInJob(process, job):
                raise JobIsolationError("suspended child not in controlled Job")
            contained = True
            if win32process.ResumeThread(thread) != 1:
                raise JobIsolationError("primary thread did not resume exactly once")
            thread.Close()
            thread = None

            while True:
                active = win32job.QueryInformationJobObject(
                    job, win32job.JobObjectBasicAccountingInformation)["ActiveProcesses"]
                max_active = max(max_active, active)
                root_done = win32event.WaitForSingleObject(process, 0) == win32event.WAIT_OBJECT_0
                if root_done and active == 0:
                    verified_empty = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    win32job.TerminateJobObject(job, 1)
                    cleanup_deadline = time.monotonic() + 5.0
                    while time.monotonic() < cleanup_deadline:
                        active = win32job.QueryInformationJobObject(
                            job, win32job.JobObjectBasicAccountingInformation)["ActiveProcesses"]
                        root_done = (win32event.WaitForSingleObject(process, 0)
                                     == win32event.WAIT_OBJECT_0)
                        if root_done and active == 0:
                            verified_empty = True
                            break
                        time.sleep(0.02)
                    if not verified_empty:
                        raise JobIsolationError("Job termination unconfirmed after timeout")
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(min(0.02, remaining))

            returncode = win32process.GetExitCodeProcess(process)
            stdout_file.seek(0)
            stderr_file.seek(0)
            result = subprocess.CompletedProcess(
                command, returncode, stdout_file.read().decode("utf-8", "replace"),
                stderr_file.read().decode("utf-8", "replace"))
            result.job_evidence = {"contained_before_resume": True,
                                   "active_processes_at_return": 0,
                                   "max_active_processes_observed": max_active,
                                   "root_pid": pid}
            return result
        except BaseException as error:
            if process is not None and not verified_empty:
                try:
                    if contained:
                        win32job.TerminateJobObject(job, 1)
                    else:
                        win32process.TerminateProcess(process, 1)
                    if win32event.WaitForSingleObject(process, 5000) != win32event.WAIT_OBJECT_0:
                        raise JobIsolationError("suspended or contained root termination unconfirmed")
                    if contained and win32job.QueryInformationJobObject(
                            job, win32job.JobObjectBasicAccountingInformation)["ActiveProcesses"] != 0:
                        raise JobIsolationError("contained descendants remain after cleanup")
                except BaseException as cleanup_error:
                    raise JobIsolationError("Job cleanup/verification failed") from cleanup_error
            if isinstance(error, (subprocess.TimeoutExpired, KeyboardInterrupt, SystemExit,
                                  JobIsolationError)):
                raise
            raise JobIsolationError("Windows Job creation or supervision failed") from error
        finally:
            if thread is not None:
                thread.Close()
            if process is not None:
                process.Close()
            if job is not None:
                job.Close()  # KILL_ON_JOB_CLOSE also covers abnormal parent exit.
