#!/usr/bin/env python3
"""Isolated real smoke: new test volume, blank page, no user login or publication."""
import json
import re
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
import os
import fcntl

ROOT = Path(__file__).resolve().parent
name = 'codexpp-smoke-' + uuid.uuid4().hex[:10]
volume = name + '-data'
result = {'image': os.environ.get('CODEXPP_IMAGE', 'codex-plus-plus:latest'), 'checks': {}, 'started_utc': time.strftime('%FT%TZ', time.gmtime())}

def docker(*args, check=True):
    if args[0] == 'exec':
        args = ('exec', '--workdir', '/', *args[1:])
    p = subprocess.run(['sudo', '-n', 'docker', *args], capture_output=True, text=True,
                       timeout=90 if args[0] == 'stop' else 60)
    if check and p.returncode:
        raise RuntimeError('docker operation failed: ' + args[0])
    return p

def inside(source):
    return docker('exec', name, 'python3', '-c', source).stdout.strip()

def status():
    return json.loads(docker('exec', name, 'python3', '/opt/codexpp/bin/browser.py', 'status').stdout)

try:
    with tempfile.TemporaryDirectory(prefix='codexpp-smoke-') as workspace:
        Path(workspace, 'README.md').write_text('Isolated codex++ browser smoke.\n')
        print('[1/4] starting isolated container', flush=True)
        args = ['run', '-d', '--name', name, '--network', 'host', '--memory', '1500m',
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges=true', '--pids-limit', '512',
                '--security-opt', 'seccomp=' + str(ROOT / 'security/codexpp-seccomp.json'),
                '--security-opt', 'apparmor=codexpp-sandbox',
                '-e', 'MCP_PORT=48772', '-e', 'CODEXPP_LOCK_WAIT=45',
                '-e', 'CODEXPP_CDP_PORT=19224', '-e', 'CODEXPP_VNC_PORT=15902', '-e', 'CODEXPP_NOVNC_PORT=16082',
                '-e', 'CODEXPP_BROWSER_LIFETIME=120', '-v', workspace + ':/workspace',
                '-v', volume + ':/data', '-v', '/run/lock/browser-fleet:/run/lock/browser-fleet:ro']
        if os.environ.get('CODEXPP_EXTERNAL_DISPLAY') == '1' and Path('/tmp/.X11-unix/X99').exists():
            args += ['-v', '/tmp/.X11-unix:/tmp/.X11-unix:ro', '-e', 'DISPLAY=:99', '-e', 'CODEXPP_EXTERNAL_DISPLAY=1']
        docker(*args, result['image'])
        for _ in range(20):
            p = docker('exec', name, 'curl', '-fs', 'http://127.0.0.1:48772/health', check=False)
            if p.returncode == 0:
                result['checks']['mcp_health'] = True
                break
            time.sleep(.5)
        else:
            raise RuntimeError('MCP health timeout')
        protocol = inside('import json,urllib.request; from pathlib import Path; s=Path("/data/state/mcp.secret").read_text().strip(); url="http://127.0.0.1:48772/mcp/"+s; results=[];\nfor method in ("initialize","tools/list"):\n payload={"jsonrpc":"2.0","id":1,"method":method,"params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"codexpp-smoke","version":"1"}}}; req=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={"Content-Type":"application/json","User-Agent":"codexpp-doctor/smoke"}); response=json.load(urllib.request.urlopen(req,timeout=5)); results.append("result" in response)\nprint(json.dumps(results))')
        result['checks']['mcp_initialize_and_tools_list'] = json.loads(protocol) == [True, True]
        result['checks']['codex_sandbox_boundary'] = docker('exec', name, 'python3', '/opt/codexpp/bin/check_sandbox.py').returncode == 0
        print('[2/4] launching blank-page Chrome through host lease', flush=True)
        docker('exec', name, 'python3', '/opt/codexpp/bin/browser.py', 'start', '--url', 'about:blank')
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            s = status()
            if s['state'] == 'running':
                break
            if s['state'] == 'stopped':
                raise RuntimeError('Chrome stopped before ready')
            time.sleep(.5)
        else:
            raise RuntimeError('Chrome readiness/lease timeout')
        result['checks']['chrome_running'] = True
        result['checks']['novnc_health'] = inside('import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:16082/vnc.html",timeout=5).status)') == '200'
        held = inside('import fcntl; f=open("/run/lock/browser-fleet/chrome.lock","r");\ntry:\n fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB); print("free")\nexcept BlockingIOError:\n print("held")')
        result['checks']['shared_host_lock_held'] = held == 'held'
        profile = inside('from pathlib import Path; p=Path("/data/chrome-profile"); print(oct(p.stat().st_mode & 0o777))')
        result['checks']['dedicated_profile_mode_700'] = profile == '0o700'
        print('[3/4] killing browser controller and checking watchdog cleanup', flush=True)
        inside('import os,signal; os.kill(' + str(s['pid']) + ',signal.SIGKILL)')
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            released = inside('import fcntl; f=open("/run/lock/browser-fleet/chrome.lock","r");\ntry:\n fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB); print("free")\nexcept BlockingIOError:\n print("held")')
            if released == 'free':
                break
            time.sleep(.2)
        result['checks']['sigkill_controller_releases_lease'] = released == 'free'
        if released != 'free':
            raise RuntimeError('watchdog did not release browser lease')
        docker('exec', name, 'python3', '/opt/codexpp/bin/browser.py', 'start', '--url', 'about:blank')
        deadline = time.monotonic() + 35
        while status()['state'] != 'running' and time.monotonic() < deadline:
            time.sleep(.2)
        result['checks']['restart_after_sigkill'] = status()['state'] == 'running'
        print('[3/4] stopping Chrome and verifying lease release', flush=True)
        docker('exec', name, 'python3', '/opt/codexpp/bin/browser.py', 'stop')
        result['checks']['chrome_stopped'] = status()['state'] == 'stopped'
        unlocked = inside('import fcntl; f=open("/run/lock/browser-fleet/chrome.lock","r");\ntry:\n fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB); print("free")\nexcept BlockingIOError:\n print("held")')
        result['checks']['shared_host_lock_released'] = unlocked == 'free'
        if unlocked != 'free':
            holders = inside('import json; from pathlib import Path; rows=[];\nfor p in Path("/proc").iterdir():\n if not p.name.isdecimal(): continue\n try:\n  fds=[f.name for f in (p/"fd").iterdir() if "chrome.lock" in str(f.readlink())]; fields=(p/"stat").read_text().rsplit(")",1)[1].split()\n  if fds: rows.append({"pid":int(p.name),"comm":(p/"comm").read_text().strip(),"ppid":int(fields[1]),"pgrp":int(fields[2]),"fds":fds})\n except OSError: pass\nprint(json.dumps(rows))')
            result['remaining_lock_handles'] = json.loads(holders)
        versions = docker('exec', name, 'sh', '-c', 'google-chrome-stable --version; codex --version').stdout.strip()
        result['versions'] = versions.splitlines()
        docker('exec', name, 'python3', '/opt/codexpp/bin/browser.py', 'start', '--url', 'about:blank')
        deadline = time.monotonic() + 35
        while status()['state'] != 'running' and time.monotonic() < deadline:
            time.sleep(.2)
        if status()['state'] != 'running':
            raise RuntimeError('Chrome not ready for container shutdown check')
        docker('stop', '--time', '75', name)
        result['checks']['graceful_container_exit'] = docker('inspect', '--format', '{{.State.ExitCode}}', name).stdout.strip() == '0'
        with Path('/run/lock/browser-fleet/chrome.lock').open('r') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result['checks']['container_stop_releases_lease'] = True
            except BlockingIOError:
                result['checks']['container_stop_releases_lease'] = False
        if not all(result['checks'].values()):
            raise RuntimeError('smoke check failed')
        result['passed'] = True
except Exception as exc:
    result['passed'] = False
    result['error'] = str(exc)
    diagnostic = docker('exec', name, 'tail', '-40', '/data/state/browser.log', check=False)
    result['browser_diagnostics'] = re.sub(r'(?:https?|wss?)://\S+', '<url>', diagnostic.stdout).splitlines()
finally:
    print('[4/4] cleaning only isolated test container and volume', flush=True)
    docker('exec', name, 'python3', '/opt/codexpp/bin/browser.py', 'stop', check=False)
    docker('rm', '-f', name, check=False)
    docker('volume', 'rm', volume, check=False)
    result['finished_utc'] = time.strftime('%FT%TZ', time.gmtime())
    (ROOT / 'PHASE2_SMOKE_RESULT.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2), flush=True)
raise SystemExit(0 if result['passed'] else 1)
