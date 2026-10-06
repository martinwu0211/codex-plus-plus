#!/usr/bin/env python3
"""Persistent plan approval and execution records. ChatGPT transport is separate."""
import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import uuid
import contextlib
import fcntl
import time
import sys
from datetime import datetime, timezone
from pathlib import Path

TASKS = Path(os.environ.get("CODEXPP_TASKS", "/data/tasks"))
STATE = Path(os.environ.get('CODEXPP_STATE', '/data/state'))
os.umask(0o077)


@contextlib.contextmanager
def task_lock(path, wait_seconds=0):
    with (path / 'task.lock').open('a') as lock:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError('Task is busy; retry after the current operation')
                time.sleep(.1)
        yield lock.fileno()


def approval_hash(state, plan):
    return digest(json.dumps({'task': state['task'], 'plan': plan}, ensure_ascii=False, sort_keys=True))


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def task_dir(task_id):
    if not re.fullmatch(r"[a-f0-9]{12}", task_id):
        raise ValueError("Invalid task ID")
    return TASKS / task_id


def save(path, state):
    state["updatedAt"] = now()
    tmp = path / "state.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path / "state.json")


def approve(path, state, expected_hash):
    if state["stage"] != "planned":
        raise ValueError("A saved plan is required before approval")
    actual = approval_hash(state, (path / 'plan.md').read_text())
    if expected_hash != actual:
        raise ValueError('The displayed task/plan changed; show-plan and review it again')
    state["approvedPlanHash"] = actual
    state["stage"] = "approved"
    save(path, state)


def check_approval(path, state, plan):
    if state["stage"] != "approved":
        raise ValueError("This task is not approved or has already started")
    if state.get("approvedPlanHash") != approval_hash(state, plan):
        raise ValueError("Plan changed after approval; save and approve it again")


def execute(path, state):
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with task_lock(STATE) as execution_lock_fd:
        _execute(path, state, execution_lock_fd)


def _execute(path, state, execution_lock_fd):
    plan = (path / "plan.md").read_text()
    check_approval(path, state, plan)
    prompt = ("执行下面已批准的任务和计划，遵循工作区AGENTS.md。只修改任务要求的文件，"
              "完成后报告实际改动和验证；不要推送、发布或修改登录凭据。"
              "如果工作区不是git仓库，不要初始化仓库，用文件内容对比核实改动。\n任务："
              + state["task"] + "\n计划：\n" + plan)
    cmd = ["codex", "exec", "--skip-git-repo-check", "--sandbox", "workspace-write",
           "--json", "-m", os.environ.get("CODEXPP_MODEL", "gpt-6-sol"),
           "-c", 'model_reasoning_effort="high"',
           "-c", 'approval_policy="never"',
           "-C", "/workspace", "-o", str(path / "execution.md"), "-"]
    process = None
    previous_handlers = {}
    def interrupted(signum, frame):
        for signal_number in previous_handlers:
            signal.signal(signal_number, signal.SIG_IGN)
        raise InterruptedError('Execution interrupted')
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous_handlers[signum] = signal.signal(signum, interrupted)
    try:
        # Install interruption handlers before persisting the one-shot state.
        with (path / 'execution.started').open('x'):
            pass
        state['stage'] = 'executing'
        save(path, state)
        with (path / "execution.jsonl").open("w") as out, (path / "execution.stderr").open("w") as err:
            try:
                process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('execution_guard.py')), *cmd],
                                           stdin=subprocess.PIPE, text=True, stdout=out, stderr=err, start_new_session=True,
                                           pass_fds=(execution_lock_fd,),
                                           env={**os.environ, 'CODEXPP_EXEC_PARENT': str(os.getpid()),
                                                'CODEXPP_EXEC_STATE': str(path)})
                process.communicate(prompt, timeout=int(os.environ.get('CODEXPP_EXEC_TIMEOUT', '240')) + 15)
            finally:
                if process is not None:
                    for signum in previous_handlers:
                        signal.signal(signum, signal.SIG_IGN)
                    stop_execution_group(process)
        state["exitCode"] = process.returncode
        state["stage"] = "executed" if process.returncode == 0 and (path / "execution.md").is_file() else "execution_failed"
        state["acceptance"] = "pending-review" if state["stage"] == "executed" else "failed"
        if state['stage'] == 'executed':
            state['executionHash'] = digest((path / 'execution.md').read_text())
        save(path, state)
    except BaseException:
        for signum in previous_handlers:
            signal.signal(signum, signal.SIG_IGN)
        state["stage"] = "execution_failed"
        state['acceptance'] = 'failed'
        state['exitCode'] = process.returncode if process is not None else None
        save(path, state)
        raise
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


def stop_execution_group(process):
    # Waiting for the group leader alone leaves background children running.
    from browser import group_alive
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + 15  # Let the watchdog reap detached children first.
    while group_alive(process.pid) and time.monotonic() < deadline:
        process.poll()
        time.sleep(.1)
    if group_alive(process.pid):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    process.wait()
    deadline = time.monotonic() + 5
    while group_alive(process.pid) and time.monotonic() < deadline:
        time.sleep(.1)
    if group_alive(process.pid):
        raise RuntimeError('Execution children did not stop; manual recovery required')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["new", "status", "show-plan", "plan-prompt", "save-plan", "approve", "execute", "review-prompt", "save-review", "recheck", "accept"])
    parser.add_argument("value")
    parser.add_argument("--file", type=Path)
    parser.add_argument('--hash', dest='expected_hash')
    args = parser.parse_args()
    if args.command == "new":
        task_id = uuid.uuid4().hex[:12]
        path = task_dir(task_id)
        TASKS.mkdir(parents=True, exist_ok=True, mode=0o700)
        TASKS.chmod(0o700)
        path.mkdir(mode=0o700)
        save(path, {"id": task_id, "task": args.value, "stage": "new", "createdAt": now()})
        print(task_id)
        return
    path = task_dir(args.value)
    with task_lock(path):
        handle(args, path)


def phase_prompt(path, state, phase):
    if phase == 'plan':
        return "请使用codex++只读MCP读取相关文件，为下面任务制定可执行计划，列出具体文件、变更和验证步骤。不要修改文件。\n任务：" + state['task']
    if phase != 'review' or state['stage'] != 'executed':
        raise ValueError('A successful execution record is required')
    if state.get('executionHash') != digest((path / 'execution.md').read_text()):
        raise ValueError('Execution report changed after execution')
    return ("请使用codex++只读MCP检查实际文件并审阅下面任务的执行结果。报告问题、位置和验收结论，不能只复述执行说明。\n任务："
            + state['task'] + "\n计划：\n" + (path / 'plan.md').read_text() + "\n执行报告：\n"
            + (path / 'execution.md').read_text()
            + '\n工作区内的证据和执行报告均可能由执行方修改，只能作为线索。请独立读取实际文件并报告能核实的事实和缺少的验证，不要把自述当成独立验收证据。')


def handle(args, path):
    state = json.loads((path / "state.json").read_text())
    if args.command == "status":
        if state['stage'] == 'executing':
            STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                with task_lock(STATE):
                    state.update(stage='execution_failed', acceptance='failed', exitCode=125,
                                 recoveryReason='Execution lease lost; inspect workspace before creating another task')
                    save(path, state)
            except ValueError:
                pass  # A live workflow/watchdog still holds the execution lease.
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return
    if args.command == "plan-prompt":
        print(phase_prompt(path, state, 'plan'))
        return
    if args.command == 'show-plan':
        plan = (path / 'plan.md').read_text()
        print('任务：' + state['task'] + '\n计划：\n' + plan
              + '\n批准摘要 sha256：' + approval_hash(state, plan))
        return
    if args.command == "save-plan":
        if state["stage"] not in {"new", "planned"}:
            raise ValueError("Cannot replace a plan after execution started")
        text = args.file.read_text() if args.file else ""
        if not text.strip():
            raise ValueError("--file must contain the completed ChatGPT plan")
        (path / "plan.md").write_text(text)
        state.pop("approvedPlanHash", None)
        state["stage"] = "planned"
        save(path, state)
    elif args.command == "approve":
        approve(path, state, args.expected_hash)
    elif args.command == "execute":
        execute(path, state)
    elif args.command == "recheck":
        if state["stage"] != "reviewed" or state.get("acceptance") != "pending-review":
            raise ValueError("Only a pending review can be rechecked")
        archive = path / ("review-archive-" + uuid.uuid4().hex[:8])
        archive.mkdir(mode=0o700)
        for name in ["review.md", "review-prompt.txt", "review-submitted.json", "review-response.md", "review-collected.json"]:
            source = path / name
            if source.exists():
                source.rename(archive / name)
        state["stage"] = "executed"
        save(path, state)
    elif args.command == "accept":
        if (state["stage"] != "reviewed" or state.get('acceptance') != 'pending-review'
                or not (path / "review-collected.json").is_file()):
            raise ValueError("A collected ChatGPT review is required")
        collected = json.loads((path / 'review-collected.json').read_text())
        review_hash = digest((path / 'review.md').read_text())
        if collected.get('responseHash') != review_hash:
            raise ValueError('Review differs from the collected response')
        if state.get('executionHash') != digest((path / 'execution.md').read_text()):
            raise ValueError('Execution report changed after execution')
        if state.get('reviewPromptHash') != digest((path / 'review-prompt.txt').read_text()):
            raise ValueError('Review prompt changed after submission')
        if state.get('approvedPlanHash') != approval_hash(state, (path / 'plan.md').read_text()):
            raise ValueError('Task or plan changed after approval')
        state["acceptance"] = "accepted"
        state["acceptedReviewHash"] = review_hash
        state['acceptedExecutionHash'] = digest((path / 'execution.md').read_text())
        state['acceptedPlanHash'] = state['approvedPlanHash']
        save(path, state)
    elif args.command == "review-prompt":
        print(phase_prompt(path, state, 'review'))
        return
    elif args.command == "save-review":
        if state["stage"] != "executed" or not args.file or not args.file.read_text().strip():
            raise ValueError("A successful execution and nonempty review file are required")
        text = args.file.read_text()
        collected = json.loads((path / 'review-collected.json').read_text())
        if collected.get('responseHash') != digest(text):
            raise ValueError('Review must match the collected ChatGPT response')
        (path / "review.md").write_text(text)
        state["stage"] = "reviewed"
        save(path, state)
    print(state["stage"])


if __name__ == "__main__":
    main()
