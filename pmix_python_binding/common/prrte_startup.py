"""Bound PRRTE readiness waits while retaining both output streams on disk."""

import math
import os
from pathlib import Path
import signal
import subprocess
import time


def start_prrte(command, log_prefix, timeout=120.0):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("PRRTE startup timeout must be finite and positive")
    stdout_path = Path(str(log_prefix) + ".prte.stdout")
    stderr_path = Path(str(log_prefix) + ".prte.stderr")
    # Regular files avoid pipe backpressure and preserve partial lines even
    # when the controller is killed by the allocation's time limit.
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr,
                                   start_new_session=True)
    started = time.monotonic()
    reason = "PRRTE STARTUP TIMEOUT"
    try:
        pending = b""
        with stdout_path.open("rb") as output:
            while time.monotonic() - started < timeout:
                chunk = output.read(65536)
                pending += chunk
                lines = pending.split(b"\n")
                pending = lines.pop()[-65536:]
                if any(line.strip() == b"DVM ready" for line in lines):
                    if process.poll() is None:
                        return process
                if process.poll() is not None:
                    reason = "PRRTE EXITED BEFORE READINESS (exit={})".format(process.returncode)
                    break
                time.sleep(0.05)
    except BaseException:
        _stop_process_group(process)
        raise
    _stop_process_group(process)
    diagnostics = []
    for path in (stdout_path, stderr_path):
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 8192))
            diagnostics.append("{}:\n{}".format(path, stream.read().decode("utf-8", errors="replace")))
    raise RuntimeError("{} after {:.1f}s; timeout={}s\n{}".format(
        reason, time.monotonic() - started, timeout, "\n".join(diagnostics)))


def _stop_process_group(process):
    # The child owns a new session; never signal the controller's process group.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        if sig == signal.SIGTERM:
            time.sleep(0.1)
    process.wait(timeout=5)
