"""Real fake-child probes: no Codex model, account or workspace access."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

GUARD = str(Path(__file__).with_name('execution_guard.py'))


def alive(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except OSError:
        return False


class ExecutionSupervision(unittest.TestCase):
    def test_sigkill_guard_stops_direct_command(self):
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / 'child.pid'
            command = f'import os,time; from pathlib import Path; Path({str(record)!r}).write_text(str(os.getpid())); time.sleep(60)'
            guardian = subprocess.Popen([sys.executable, GUARD, sys.executable, '-c', command])
            self.addCleanup(lambda: guardian.kill() if guardian.poll() is None else None)
            deadline = time.monotonic() + 3
            while not record.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(record.exists())
            pid = int(record.read_text())
            self.addCleanup(lambda: os.kill(pid, signal.SIGKILL) if alive(pid) else None)
            guardian.kill()
            guardian.wait()
            deadline = time.monotonic() + 3
            while alive(pid) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertFalse(alive(pid))

    def test_normal_exit_reaps_detached_background_child(self):
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / 'child.pid'
            child_code = 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'
            command = ('import subprocess,sys,time; from pathlib import Path; '
                       f'p=subprocess.Popen([sys.executable,"-c",{child_code!r}],start_new_session=True); '
                       f'Path({str(record)!r}).write_text(str(p.pid)); time.sleep(.3)')
            result = subprocess.run([sys.executable, GUARD, sys.executable, '-c', command], timeout=12)
            pid = int(record.read_text())
            self.addCleanup(lambda: os.kill(pid, signal.SIGKILL) if alive(pid) else None)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(alive(pid))

    def test_killed_caller_still_stops_guarded_child(self):
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / 'child.pid'
            child_code = f'import os,time; from pathlib import Path; Path({str(record)!r}).write_text(str(os.getpid())); time.sleep(60)'
            caller_code = f'import subprocess,sys,time; subprocess.Popen([sys.executable,{GUARD!r},sys.executable,"-c",{child_code!r}]); time.sleep(60)'
            caller = subprocess.Popen([sys.executable, '-c', caller_code])
            self.addCleanup(lambda: caller.kill() if caller.poll() is None else None)
            deadline = time.monotonic() + 3
            while not record.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(record.exists())
            pid = int(record.read_text())
            self.addCleanup(lambda: os.kill(pid, signal.SIGKILL) if alive(pid) else None)
            caller.kill()
            caller.wait()
            deadline = time.monotonic() + 8
            while alive(pid) and time.monotonic() < deadline:
                time.sleep(.1)
            self.assertFalse(alive(pid))
