#!/usr/bin/env python3
"""Linux execution watchdog; survives a lost caller and reaps detached children."""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def descendants():
    rows = {}
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] != 'Z':
                rows[int(path.name)] = (int(fields[1]), fields[19])
        except (OSError, IndexError, ValueError):
            pass
    found, parents = {}, {os.getpid()}
    while True:
        children = {pid: row for pid, row in rows.items() if row[0] in parents and pid not in found}
        if not children:
            return found
        found.update(children)
        parents = set(children)


def kill_descendants(signum):
    for pid, (_, ticks) in descendants().items():
        try:
            current = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
            if current == ticks:
                os.kill(pid, signum)
        except (OSError, IndexError):
            pass


def main():
    # Detached grandchildren are reparented here instead of escaping to PID 1.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0):  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'Cannot establish execution supervision')
    parent = int(os.environ.get('CODEXPP_EXEC_PARENT', str(os.getppid())))
    # Browser start intentionally returns; only this fixed service may detach.
    detached_browser = (len(sys.argv) > 3 and Path(sys.argv[1]).resolve() == Path(sys.executable).resolve()
                        and Path(sys.argv[2]).resolve() == Path(__file__).with_name('browser.py').resolve()
                        and sys.argv[3] == 'serve')
    if not detached_browser and os.getppid() != parent:
        return 124
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop)
    guardian_pid = os.getpid()
    def bind_parent_lifetime():
        # Keep the command in the caller's supervised group. A killed guardian
        # must also stop its direct command, including the pre-exec race.
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0):
            os._exit(124)
        if os.getppid() != guardian_pid:
            os._exit(124)
    process = subprocess.Popen(sys.argv[1:], preexec_fn=bind_parent_lifetime)
    deadline = time.monotonic() + int(os.environ.get('CODEXPP_EXEC_TIMEOUT', '240'))
    failed = False
    try:
        while process.poll() is None:
            if stopping or (not detached_browser and os.getppid() != parent) or time.monotonic() >= deadline:
                failed = True
                break
            time.sleep(.1)
    finally:
        kill_descendants(signal.SIGTERM)
        cleanup_deadline = time.monotonic() + 5
        while descendants() and time.monotonic() < cleanup_deadline:
            process.poll()
            time.sleep(.1)
        kill_descendants(signal.SIGKILL)
        process.wait()
        cleanup_deadline = time.monotonic() + 5
        while descendants() and time.monotonic() < cleanup_deadline:
            kill_descendants(signal.SIGKILL)
            time.sleep(.1)
        if descendants():
            failed = True
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
            except ChildProcessError:
                break
    if not detached_browser and os.getppid() != parent and os.environ.get('CODEXPP_EXEC_STATE'):
        from workflow import task_lock, save
        path = Path(os.environ['CODEXPP_EXEC_STATE'])
        with task_lock(path, wait_seconds=5):
            state = json.loads((path / 'state.json').read_text())
            if state.get('stage') == 'executing':
                state.update(stage='execution_failed', acceptance='failed', exitCode=124)
                save(path, state)
    return 124 if failed else process.returncode


if __name__ == '__main__':
    raise SystemExit(main())
