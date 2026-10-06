#!/usr/bin/env python3
"""Run a read-only Claude audit and persist its result; never push Git."""
import hashlib
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs' / 'audit'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--section', choices=('mcp', 'docker', 'workflow', 'browser', 'release', 'remediation', 'all'), default='mcp')
    section = parser.parse_args().section
    os.umask(0o077)
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    report = OUT / ('CLAUDE_' + section.upper() + '_' + stamp + '.md')
    running = report.with_suffix('.running')
    status = report.with_suffix('.status.json')
    git = ['git', '-c', 'safe.directory=' + str(ROOT), '-C', str(ROOT)]
    candidates = subprocess.check_output(git + ['ls-files', '-z']).decode().split('\0')
    candidates += subprocess.check_output(git + ['ls-files', '--others', '--exclude-standard', '-z']).decode().split('\0')
    candidates = sorted(set(candidates))
    files = [name for name in candidates if name]
    groups = {
        'remediation': ['image/bridge/server.py', 'THIRD_PARTY.md', 'CHARTER.md',
                        'docs/audit/BUILD_VERIFICATION.md', 'docs/audit/MCP_REMEDIATION.md',
                        'scripts/run_claude_audit.py'],
        'mcp': ['image/bridge/server.py', 'image/bridge/test_workspace_security.py',
                'image/bridge/test_protocol_compat.py', 'docs/audit/MCP_REMEDIATION.md'],
        'docker': ['image/Dockerfile', 'image/.dockerignore', 'codex++', 'build_and_test.sh',
                   'image/bin/supervisor.sh', 'image/bin/check_sandbox.py', 'security/generate.py',
                   'security/codexpp.apparmor', 'security/codexpp-seccomp.json', 'image/dependencies.json',
                   'image/bin/install_dependency.py', 'README.md'],
        'workflow': ['image/bin/browser.py', 'image/bin/test_browser.py', 'image/bin/workflow.py',
                     'image/bin/test_workflow.py', 'image/bin/chat_task.py', 'image/bin/connector.mjs',
                     'image/bin/execution_guard.py', 'image/bin/test_execution_guard.py'],
        'browser': ['image/bin/browser.py', 'image/bin/test_browser.py',
                    'image/bin/execution_guard.py', 'image/bin/test_execution_guard.py'],
        'release': ['README.md', 'CHARTER.md', '.gitignore', 'image/.dockerignore',
                    'image/Dockerfile', 'image/dependencies.json', 'build_and_test.sh', 'security/generate.py',
                    'THIRD_PARTY.md', 'image/bin/doctor.py', 'scripts/lock_dependencies.py',
                    'scripts/verify_live_mcp.py', 'smoke_phase2.py', 'scripts/run_claude_audit.py',
                    'image/bin/install_dependency.py', 'security/apparmor-upstream.go',
                    'security/seccomp-upstream.json', 'security/LICENSE.moby',
                    'LICENSE', 'security/LICENSE.codex-with-chatgpt',
                    'docs/audit/BUILD_VERIFICATION.md', 'docs/audit/MCP_REMEDIATION.md'],
    }
    if section != 'all':
        files = groups[section]
    files = [name for name in files if not name.startswith('docs/audit/CLAUDE_')]
    # Preserve publication scope without copying runtime data or private report text.
    candidate_paths = [name for name in candidates if name and not name.startswith('docs/audit/CLAUDE_')]
    manifest = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files}
    metadata = {'started_at_utc': datetime.now(timezone.utc).isoformat(),
                'state': 'running', 'section': section, 'model': 'claude-opus-5-5', 'candidate_sha256': manifest}
    status.write_text(json.dumps(metadata, indent=2) + '\n')
    prompt = ((OUT / 'REQUEST.md').read_text()
              + '\n本轮仅审计 ' + section + ' 段。全部待审源码已在下面给出，无需调用工具。'
              + 'docker 范围为第2和第5段，workflow 范围为第3和第4段，browser 为第3段，release 为第5和第6段。'
              + '仅按本轮范围给结论，其他段注明未审；不要仅因其他段未审而否定本段整改。'
              + '报告保持精简（不超过1200中文字），优先列可复现的高/中风险，低风险合并。不要复述整个源码。\n')
    if section == 'release':
        prompt += '\nGit候选文件路径（不含内容）：\n' + '\n'.join(candidate_paths) + '\n'
    if section == 'all':
        prompt += '\n本轮覆盖第1至6段及全部Git候选文件，重点核查残留上传阻断项和凭据、个人标识；允许注明内部测试版本的已知限制，不把未声称的能力当成已验收。\n'
    if section == 'remediation':
        previous = OUT / 'CLAUDE_ALL_20261006T100253Z.md'
        prompt += '\n本轮只复核此前全量审计的阻断项M1来源说明、M2最终镜像实测以及整改文档状态。前次其余源码保持冻结，M3/M4及其他中低事项保留为已知限制。请判定是否允许私有仓库源码候选，公开许可仍未确认。前次总审报告：\n' + previous.read_text()
    for name in files:
        prompt += '\n--- FILE: ' + name + ' ---\n' + (ROOT / name).read_text() + '\n--- END FILE ---\n'
    command = [shutil.which('claude') or 'claude', '-p', '--model', metadata['model'],
               '--output-format', 'text', '--effort', 'medium', '--tools', '', '--permission-mode', 'dontAsk',
               '--safe-mode', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--']
    private_logs = ROOT / '.local' / 'audit'
    private_logs.mkdir(parents=True, exist_ok=True)
    try:
        with running.open('w') as output, (private_logs / (stamp + '.stderr.log')).open('w') as errors:
            result = subprocess.run(command, cwd=ROOT, input=prompt, text=True, stdout=output,
                                    stderr=errors, timeout=240)
        metadata['exit_code'] = result.returncode
        metadata['state'] = 'completed' if result.returncode == 0 and running.stat().st_size else 'failed'
        if metadata['state'] == 'completed':
            running.replace(report)
            metadata['report'] = report.name
    except subprocess.TimeoutExpired:
        metadata['state'] = 'timed_out'
    except OSError as exc:
        metadata['state'] = 'failed'
        metadata['error_type'] = type(exc).__name__
    metadata['ended_at_utc'] = datetime.now(timezone.utc).isoformat()
    metadata['source_unchanged'] = all(
        (ROOT / name).is_file() and hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
        for name, digest in manifest.items())
    status.write_text(json.dumps(metadata, indent=2) + '\n')
    print(json.dumps({k: v for k, v in metadata.items() if k != 'candidate_sha256'}))
    return 0 if metadata['state'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
