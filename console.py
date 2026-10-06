#!/usr/bin/env python3
"""codex++ 交互入口：ChatGPT 规划 → 输入「执行」批准 → 容器内 Codex 执行 → ChatGPT 审阅。

由宿主命令 `codex++` 在没有管理子命令时调用；所有容器操作都经由 `codex++` 本身完成。"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

HOST_COMMAND = Path(__file__).resolve().with_name('codex++')


def call(*args):
    result = subprocess.run([str(HOST_COMMAND), *args], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'Docker command failed')
    return result.stdout.strip()


def state(task_id):
    return json.loads(call('task', 'status', task_id))


def workspace():
    source = call('workspace')
    if not source:
        raise RuntimeError('容器没有挂载 /workspace；先运行 codex++ up')
    return Path(source).resolve()


def has_task_file(task_id, name):
    result = subprocess.run([str(HOST_COMMAND), 'task-file', task_id, name], capture_output=True)
    return result.returncode == 0


def wait_for(task_id, phase):
    wanted = 'planned' if phase == 'plan' else 'reviewed'
    # Persisted task IDs let another invocation continue after interruption.
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        print(call('chat', 'poll', task_id, phase), flush=True)
        current = state(task_id)['stage']
        if current == wanted:
            return
        if current not in {'new', 'executed'}:
            raise RuntimeError('Unexpected task stage: ' + current)
        time.sleep(3)
    raise RuntimeError(f'ChatGPT is still working. Continue with: codex++ resume {task_id}')


def read_task_file(task_id, name):
    return call('task-file', task_id, name)


def proceed(task_id):
    current = state(task_id)['stage']
    if current == 'new':
        submitted = has_task_file(task_id, 'plan-submitted.json')
        if not submitted:
            print(call('chat', 'start', task_id, 'plan'), flush=True)
        wait_for(task_id, 'plan')
        current = 'planned'
    if current == 'planned':
        shown = call('task', 'show-plan', task_id)
        print(shown, flush=True)
        digest = shown.rsplit('批准摘要 sha256：', 1)[-1].strip()
        if not sys.stdin.isatty():
            print(f'Plan saved. Review it, then run: codex++ resume {task_id}')
            return
        answer = input('输入「执行」批准以上具体计划；其他输入保留任务并退出：').strip()
        if answer != '执行':
            return
        call('task', 'approve', task_id, '--hash', digest)
        current = 'approved'
    if current == 'approved':
        print('Codex 正在执行已批准的计划……', flush=True)
        print(call('task', 'execute', task_id), flush=True)
        current = state(task_id)['stage']
    if current == 'executed':
        print(read_task_file(task_id, 'execution.md'), flush=True)
        submitted = has_task_file(task_id, 'review-submitted.json')
        if not submitted:
            print(call('chat', 'start', task_id, 'review'), flush=True)
        wait_for(task_id, 'review')
        current = 'reviewed'
    if current == 'reviewed':
        print(read_task_file(task_id, 'review.md'), flush=True)
        print(f'审阅已保存，任务 {task_id}，最终验收状态：{state(task_id).get("acceptance", "pending-review")}')
    elif current not in {'planned', 'approved'}:
        print(json.dumps(state(task_id), ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description='ChatGPT plans/reviews; Docker Codex executes.')
    parser.add_argument('-C', '--cd', type=Path)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('request', nargs='*')
    args = parser.parse_args()
    if args.request[:1] == ['resume']:
        if len(args.request) != 2:
            parser.error('resume requires one task ID')
        proceed(args.request[1])
        return
    target = workspace()
    print('codex++ · ChatGPT 规划/审阅 → Docker Codex 执行', flush=True)
    print('当前工作区：' + str(target), flush=True)
    if args.cd and args.cd.resolve() != target:
        raise RuntimeError('指定的项目没有挂载到当前容器，未提交任务；请用 CODEXPP_WORKSPACE 重新 codex++ up')
    if args.check:
        print(call('status'))
        return
    task = ' '.join(args.request).strip()
    if not task:
        if not sys.stdin.isatty():
            parser.error('provide a task or use an interactive terminal')
        task = input('任务：').strip()
    if not task:
        return
    task_id = call('task', 'new', task)
    print(f'任务 {task_id}；可用 codex++ resume {task_id} 继续。', flush=True)
    proceed(task_id)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
