#!/usr/bin/env python3
"""Verify real Codex sandbox write policy without model calls or login reads."""
import json
import subprocess
import uuid
from pathlib import Path

name = ".codexpp-probe-" + uuid.uuid4().hex
for directory in ('/data/state', '/data/codex-home', '/data/tasks'):
    Path(directory).mkdir(mode=0o700, parents=True, exist_ok=True)
write = ("from pathlib import Path; p=Path('/workspace/" + name + "'); "
         "p.write_text('sandbox probe'); assert p.read_text()=='sandbox probe'; p.unlink()")
def blocked(directory):
    return ("from pathlib import Path; p=Path('" + directory + '/' + name + "'); "
           "\ntry:\n p.write_text('must be blocked')\nexcept OSError as e:\n if e.errno not in (13,30): raise\n print('outside workspace blocked')\nelse:\n p.unlink(); raise SystemExit('unexpected write outside workspace')")
checks = [("read-only blocks workspace writes", "read-only", blocked('/workspace'), 'outside workspace blocked'),
          ("workspace-write permits project writes", "workspace-write", write + "; print('workspace write allowed')", 'workspace write allowed'),
          *[("workspace-write blocks " + directory, "workspace-write", blocked(directory), 'outside workspace blocked')
            for directory in ('/data/state', '/data/codex-home', '/data/tasks')]]
for label, mode, code, expected in checks:
    result = subprocess.run(["codex", "sandbox", "-c", f'sandbox_mode="{mode}"', "--", "python3", "-c", code],
                            capture_output=True, text=True, timeout=20, cwd='/workspace')
    ok = result.returncode == 0 and expected in result.stdout.splitlines()
    print(json.dumps({"check": label, "ok": ok, "exit": result.returncode}))
    if not ok:
        print((result.stdout + result.stderr)[:1500])
        raise SystemExit(1)
