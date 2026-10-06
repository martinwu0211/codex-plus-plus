#!/usr/bin/env python3
"""Own one dedicated Chrome; hold the shared host lease until all children exit."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request

STATE = Path(os.environ.get('CODEXPP_STATE', '/data/state'))
PROFILE = Path(os.environ.get('CODEXPP_PROFILE', '/data/chrome-profile'))
RUNTIME = STATE / 'browser.json'
PORT = int(os.environ.get('CODEXPP_CDP_PORT', '9224'))


def start_ticks(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def current():
    try:
        data = json.loads(RUNTIME.read_text())
        if start_ticks(data['pid']) == data['start_ticks']:
            return data
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {'state': 'stopped'}


def save(state):
    tmp = RUNTIME.with_suffix('.tmp')
    tmp.write_text(json.dumps({'pid': os.getpid(), 'start_ticks': start_ticks(os.getpid()),
                               'state': state, 'cdp_port': PORT}))
    os.chmod(tmp, 0o600)
    tmp.replace(RUNTIME)


def group_alive(pgid):
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] != 'Z' and int(fields[2]) == pgid:
                return True
        except (OSError, IndexError):
            continue
    return False


def stop_group(child):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while group_alive(child.pid) and time.monotonic() < deadline:
        child.poll()
        time.sleep(.1)
    if group_alive(child.pid):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
    child.wait()
    while group_alive(child.pid):
        time.sleep(.1)


def serve(url):
    stopping = False
    def terminate(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    children = []
    lock_fd = None
    try:
        save('queued')
        lock_fd = os.open(os.environ.get('CODEXPP_CHROME_LOCK', '/run/lock/browser-fleet/chrome.lock'),
                          os.O_RDONLY | os.O_NOFOLLOW)
        deadline = time.monotonic() + int(os.environ.get('CODEXPP_LOCK_WAIT', '1800'))
        while not stopping:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('host Chrome lease timeout')
                time.sleep(.2)
        if stopping:
            return
        for address, port in (('127.0.0.1', PORT), ('127.0.0.1', int(os.environ.get('CODEXPP_VNC_PORT', '5902'))),
                              (os.environ.get('CODEXPP_WEB_BIND', '127.0.0.1'), int(os.environ.get('CODEXPP_NOVNC_PORT', '6082')))):
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((address, port))
        save('starting')
        PROFILE.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(PROFILE, 0o700)
        display = os.environ.get('DISPLAY', ':100')
        def spawn(argv):
            child = subprocess.Popen(argv, start_new_session=True, pass_fds=(lock_fd,))
            children.append(child)
            return child
        if os.environ.get('CODEXPP_EXTERNAL_DISPLAY', '0') != '1':
            xvfb = spawn(['Xvfb', display, '-screen', '0', '1440x900x24', '-ac'])
            time.sleep(1)
            if xvfb.poll() is not None:
                raise RuntimeError('Xvfb failed')
        vnc = spawn(['x11vnc', '-display', display, '-rfbport', os.environ.get('CODEXPP_VNC_PORT', '5902'),
                     '-localhost', '-nopw', '-forever', '-shared', '-quiet', '-noshm'])
        proxy = spawn(['websockify', '--web=/usr/share/novnc',
                       os.environ.get('CODEXPP_WEB_BIND', '127.0.0.1') + ':' + os.environ.get('CODEXPP_NOVNC_PORT', '6082'),
                       '127.0.0.1:' + os.environ.get('CODEXPP_VNC_PORT', '5902')])
        # Chrome uses the container boundary; it currently runs without its own sandbox.
        chrome = spawn(['/usr/bin/google-chrome-stable', '--no-sandbox', '--disable-dev-shm-usage',
                        '--no-first-run', '--no-default-browser-check', '--password-store=basic',
                        '--user-data-dir=' + str(PROFILE), '--remote-debugging-address=127.0.0.1',
                        '--remote-debugging-port=' + str(PORT), url])
        deadline = time.monotonic() + 30
        while not stopping and time.monotonic() < deadline:
            if any(c.poll() is not None for c in (chrome, vnc, proxy)):
                raise RuntimeError('browser/display process exited')
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(f'http://127.0.0.1:{PORT}/json/version', timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            time.sleep(.2)
        else:
            if not stopping:
                raise TimeoutError('Chrome CDP startup timeout')
        if stopping:
            return
        save('running')
        deadline = time.monotonic() + int(os.environ.get('CODEXPP_BROWSER_LIFETIME', '1800'))
        while not stopping and chrome.poll() is None and time.monotonic() < deadline:
            if any(c.poll() is not None for c in (vnc, proxy)):
                raise RuntimeError('remote display exited')
            time.sleep(.2)
    except Exception as exc:
        # Never include a navigation URL or session data in logs.
        print('browser failed:', type(exc).__name__, flush=True)
    finally:
        with contextlib.suppress(OSError):
            save('stopping')
        for child in reversed(children):
            stop_group(child)
        if lock_fd is not None:
            os.close(lock_fd)
        with contextlib.suppress(OSError):
            RUNTIME.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('start', 'stop', 'status', 'serve'))
    parser.add_argument('--url', default='https://chatgpt.com/')
    args = parser.parse_args()
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.command == 'serve':
        serve(args.url)
        return
    with (STATE / 'browser-control.lock').open('a') as control:
        fcntl.flock(control, fcntl.LOCK_EX)
        data = current()
        if args.command == 'start' and data['state'] == 'stopped':
            with (STATE / 'browser.log').open('a') as log:
                child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('execution_guard.py')),
                                          sys.executable, __file__, 'serve', '--url', args.url],
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                         start_new_session=True,
                                         env={**os.environ, 'CODEXPP_EXEC_STATE': '',
                                              'CODEXPP_EXEC_TIMEOUT': str(int(os.environ.get('CODEXPP_LOCK_WAIT', '1800'))
                                                                       + int(os.environ.get('CODEXPP_BROWSER_LIFETIME', '1800')) + 120)})
            for _ in range(50):
                data = current()
                if data['state'] != 'stopped' or child.poll() is not None:
                    break
                time.sleep(.1)
            if child.poll() is not None and data['state'] == 'stopped':
                raise SystemExit('Chrome startup failed; inspect browser.log')
        elif args.command == 'stop' and data['state'] != 'stopped':
            with contextlib.suppress(ProcessLookupError):
                os.kill(data['pid'], signal.SIGTERM)
            for _ in range(600):
                data = current()
                if data['state'] == 'stopped':
                    break
                time.sleep(.1)
            if data['state'] != 'stopped':
                raise SystemExit('Browser still stopping; host lease remains held')
        print(json.dumps(data))


if __name__ == '__main__':
    main()
