#!/usr/bin/env python3
"""Send/capture task phases through the logged-in dedicated Chrome."""
import argparse
import json
import subprocess
import time
from pathlib import Path
import workflow
import fcntl
import os

BIN = Path(__file__).resolve().parent
STATE = Path(os.environ.get('CODEXPP_STATE', '/data/state'))
os.umask(0o077)


def browser(action, *args):
    result = subprocess.run(["node", str(BIN / "connector.mjs"), action, *args],
                            capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError("Browser action failed: " + action)
    return result.stdout


def ensure_browser(on_start=None):
    ctl = ["python3", str(BIN / "browser.py")]
    result = subprocess.run(ctl + ["status"], capture_output=True, text=True, check=True, timeout=10)
    if json.loads(result.stdout).get("state") == "stopped":
        if on_start:
            on_start()
        subprocess.run(ctl + ["start", "--url", "https://chatgpt.com/"], capture_output=True, text=True, check=True, timeout=10)
    deadline = time.monotonic() + 20
    while True:
        try:
            browser("inspect")
            return
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise RuntimeError("Managed browser is not ready; check browser-status and shared lock")
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["start", "poll", "wait", 'reset-submit', 'abandon'])
    parser.add_argument("task_id")
    parser.add_argument("phase", choices=["plan", "review"])
    parser.add_argument('--confirmed-unsent', action='store_true')
    parser.add_argument('--confirmed-abandon', action='store_true')
    args = parser.parse_args()
    path = workflow.task_dir(args.task_id)
    if args.command == 'wait':
        finished = 'planned' if args.phase == 'plan' else 'reviewed'
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            result = subprocess.run(['python3', str(BIN / 'chat_task.py'), 'poll', args.task_id, args.phase])
            if result.returncode == 0 and json.loads((path / 'state.json').read_text())['stage'] == finished:
                return
            time.sleep(3)
        print('Still waiting; task state retained for the next poll')
        return
    with (STATE / 'chat-control.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Browser task operation is busy')
        with workflow.task_lock(path):
            handle(args, path)


def handle(args, path):
    state = json.loads((path / "state.json").read_text())
    finished = "planned" if args.phase == "plan" else "reviewed"
    expected = 'new' if args.phase == 'plan' else 'executed'
    if args.command == 'abandon':
        if not args.confirmed_abandon or state['stage'] != expected:
            raise RuntimeError('Explicit confirmation of this unfinished phase is required')
        active_file = STATE / 'chat-active.json'
        if active_file.exists() and json.loads(active_file.read_text()) != {'taskId': args.task_id, 'phase': args.phase}:
            raise RuntimeError('Another conversation owns the browser')
        state.update(stage='conversation_failed', acceptance='failed', abandonedPhase=args.phase)
        workflow.save(path, state)
        active_file.unlink(missing_ok=True)
        print('Phase abandoned; archived task retained. Create a new task to retry.')
        return
    if args.command == 'reset-submit':
        if not args.confirmed_unsent or state['stage'] != expected:
            raise RuntimeError('Inspect the browser and explicitly confirm the phase was not sent')
        submitted = path / (args.phase + '-submitted.json')
        if submitted.exists():
            record = json.loads(submitted.read_text())
            if record.get('sent'):
                raise RuntimeError('This phase was sent; poll or inspect its conversation instead')
            submitted.rename(path / (args.phase + '-uncertain-' + str(time.time_ns()) + '.json'))
        active_file = STATE / 'chat-active.json'
        if active_file.exists():
            active = json.loads(active_file.read_text())
            if active != {'taskId': args.task_id, 'phase': args.phase}:
                raise RuntimeError('Another conversation owns the browser')
            active_file.unlink()
        print('Submission reset after explicit unsent confirmation')
        return
    if args.command != 'start' and state['stage'] != expected:
        print(state['stage'])
        return
    active_file = STATE / 'chat-active.json'
    active = {'taskId': args.task_id, 'phase': args.phase}
    if active_file.exists() and json.loads(active_file.read_text()) != active:
        raise RuntimeError('Another task conversation is still in progress')
    current = (STATE / 'public_url').read_text().strip()+"/mcp/"+(STATE / 'mcp.secret').read_text().strip()
    registered = json.loads((STATE / 'connector.json').read_text())
    if not registered.get("toolsVerified") or (STATE / 'connector_url').read_text().strip() != current:
        raise RuntimeError("Current connector needs real tool verification")
    if args.command == "start":
        if (path / (args.phase + "-submitted.json")).exists():
            raise RuntimeError("Already submitted; poll instead")
        if state["stage"] != expected:
            raise RuntimeError("Unexpected task state")
        prompt = workflow.phase_prompt(path, state, args.phase)
        marker = "CODEXPP_" + args.phase.upper() + "_END_" + args.task_id
        prompt = ("任务编号：" + args.task_id + "\n请先用codex++的read_file读取README.md，确认实际内容后再回答。\n" + prompt
                  + "\n最终答复最后单独输出：" + marker)
        prompt_file = path / (args.phase + "-prompt.txt")
        prompt_file.write_text(prompt)
        prompt_file.chmod(0o600)
        if args.phase == 'review':
            state['reviewPromptHash'] = workflow.digest(prompt)
            workflow.save(path, state)
        active_file.write_text(json.dumps(active))
        started_here = False
        def remember_start():
            nonlocal started_here
            started_here = True
        try:
            ensure_browser(remember_start)
            for action in ["plugin-page", "try-chat", "work-mode"]:
                browser(action)
            browser("task-submit", args.task_id, args.phase)
        except Exception:
            # An existing marker means delivery may have happened: preserve it
            # for manual recovery. Before that point cancel our queued browser.
            if not (path / (args.phase + '-submitted.json')).exists():
                try:
                    if started_here:
                        subprocess.run(['python3', str(BIN / 'browser.py'), 'stop'],
                                       capture_output=True, text=True, check=True, timeout=65)
                except (OSError, subprocess.SubprocessError):
                    # Preserve the original failure and report failed cleanup.
                    print('Browser cleanup failed; inspect browser-status and stop manually')
                finally:
                    active_file.unlink(missing_ok=True)
            raise
        print(json.dumps({"task": args.task_id, "phase": args.phase, "state": "submitted"}))
    else:
        active_file.write_text(json.dumps(active))
        ensure_browser()
        browser("task-collect", args.task_id, args.phase)
        response = path / (args.phase + "-response.md")
        if not response.exists():
            print("waiting")
            return
        workflow.handle(argparse.Namespace(command='save-' + args.phase, value=args.task_id,
                                          file=response, expected_hash=None), path)
        active_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
