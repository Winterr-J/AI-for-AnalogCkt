"""Isolated POSIX launcher for a single, guarded SampleHold native attempt."""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path


class ProcessGroupUncertain(RuntimeError):
    """Containment or reaping could not be established; retain the run lock."""


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError as error:
        raise ProcessGroupUncertain("process group ownership is uncertain") from error
    return True


def _kill_and_reap(process: subprocess.Popen) -> None:
    # Kill the group even when the leader exited: descendants can still be alive.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as error:
        raise ProcessGroupUncertain("SIGKILL of native process group failed") from error
    try:
        process.communicate(timeout=10)
    except subprocess.TimeoutExpired as error:
        raise ProcessGroupUncertain("native child could not be reaped") from error
    if _group_exists(process.pid):
        raise ProcessGroupUncertain("native process group still exists after SIGKILL")


def run_isolated(command: list[str], *, cwd: Path, timeout: float,
                 environment: dict[str, str]) -> subprocess.CompletedProcess:
    if os.name != "posix" or timeout <= 0:
        raise ProcessGroupUncertain("POSIX containment or positive timeout unavailable")
    try:
        process = subprocess.Popen(command, cwd=cwd, env=environment, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
    except OSError as error:
        raise ProcessGroupUncertain("native child launch failed before containment") from error
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        _kill_and_reap(process)
        raise
    if _group_exists(process.pid):
        _kill_and_reap(process)
        raise ProcessGroupUncertain("native child left a live descendant")
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
