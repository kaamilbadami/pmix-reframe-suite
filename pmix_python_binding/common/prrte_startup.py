"""Bound PRRTE readiness waits while retaining both output streams on disk."""

import math
from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import subprocess
import threading
import time


def _startup_process_evidence(process, path):
    """Capture the live local PRRTE session before cleanup, without argv/env."""
    try:
        result = subprocess.run(
            ['ps', '--sid', str(process.pid), '-o', 'pid,ppid,stat,etimes,wchan:32,comm'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=3)
        path.write_text('captured_at=' + datetime.now(timezone.utc).isoformat() +
                        '\nLocal PRRTE session only; remote daemon stderr is in .prte.stderr.\n' +
                        result.stdout + result.stderr)
    except (OSError, subprocess.TimeoutExpired) as error:
        # Evidence collection must never replace the original startup failure.
        print('PRRTE process snapshot unavailable: {}'.format(error), flush=True)


def start_prrte(command, log_prefix, timeout=120.0):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("PRRTE startup timeout must be finite and positive")
    command = list(command)
    if Path(command[0]).name == 'prte' and '--debug-daemons' not in command:
        # Keep remote-daemon launch diagnostics in the existing captured streams.
        command[1:1] = ['--debug-daemons', '--leave-session-attached',
                        '--prtemca', 'plm_base_verbose', '10']
    print('PRRTE START: timestamp={} timeout={}s'.format(
        datetime.now(timezone.utc).isoformat(), timeout), flush=True)
    stdout_path = Path(str(log_prefix) + ".prte.stdout")
    stderr_path = Path(str(log_prefix) + ".prte.stderr")
    # Observe readiness on a pipe rather than by rereading a shared log file.
    # A dedicated reader keeps draining stdout even after startup returns;
    # stderr goes straight to disk so neither stream can fill an unread pipe.
    stdout = stdout_path.open("wb", buffering=0)
    try:
        with stderr_path.open("wb") as stderr:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr,
                                       start_new_session=True)
    except BaseException:
        stdout.close()
        raise
    started = time.monotonic()
    ready = threading.Event()
    observed = {}
    reader_errors = []

    def drain_stdout():
        pending = b""
        try:
            with process.stdout, stdout:
                while True:
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    pending += chunk
                    lines = pending.split(b"\n")
                    pending = lines.pop()[-65536:]
                    found_ready = not ready.is_set() and any(
                        line.strip() == b"DVM ready" for line in lines
                    )
                    if found_ready:
                        observed["received_at"] = datetime.now(timezone.utc).isoformat()
                        observed["elapsed"] = time.monotonic() - started
                    stdout.write(chunk)
                    if found_ready:
                        ready.set()
        except BaseException as error:
            reader_errors.append(error)

    reader = threading.Thread(target=drain_stdout, name="prrte-stdout", daemon=True)
    reason = "PRRTE STARTUP TIMEOUT"
    try:
        reader.start()
        while True:
            if reader_errors:
                raise RuntimeError("PRRTE stdout capture failed: {}".format(reader_errors[0]))
            if ready.is_set() and observed["elapsed"] < timeout:
                if process.poll() is None:
                    print("PRRTE READY: received_at={} elapsed={:.3f}s".format(
                        observed["received_at"], observed["elapsed"]), flush=True)
                    return process
            if process.poll() is not None:
                reason = "PRRTE EXITED BEFORE READINESS (exit={})".format(process.returncode)
                break
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                break
            ready.wait(min(0.05, remaining))
    except BaseException:
        _stop_process_group(process)
        if reader.ident is not None:
            reader.join(timeout=2)
        else:
            process.stdout.close()
            stdout.close()
        raise
    _startup_process_evidence(process, Path(str(log_prefix) + '.prte.processes.log'))
    _stop_process_group(process)
    reader.join(timeout=2)
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
