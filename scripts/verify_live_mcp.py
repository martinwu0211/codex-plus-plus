#!/usr/bin/env python3
"""Run inside codex++: fake credential tests against authenticated live MCP."""
import json
import os
from pathlib import Path
import tempfile
import urllib.request
import uuid
import subprocess

ROOT = Path(os.environ.get('MCP_WORKSPACE', '/workspace'))
STATE = Path('/data/state')


def main():
    # Keep the live credential in memory; never print it or request URLs.
    secret = (STATE / 'mcp.secret').read_text().strip()
    endpoint = 'http://127.0.0.1:' + os.environ.get('MCP_PORT', '48771') + '/mcp/' + secret
    artifacts = ROOT / '.codexpp'
    artifacts.mkdir(exist_ok=True)
    marker = 'FAKE_CREDENTIAL_' + uuid.uuid4().hex

    def call(name, arguments):
        request = urllib.request.Request(endpoint,
            data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                             'params': {'name': name, 'arguments': arguments}}).encode(),
            headers={'Content-Type': 'application/json'})
        response = json.loads(urllib.request.urlopen(request, timeout=20).read())
        return response['result']

    checks = {}
    with tempfile.TemporaryDirectory(prefix='mcp-security-', dir=artifacts) as temporary:
        folder = Path(temporary)
        relative = str(folder.relative_to(ROOT))
        for name in ('.env', '.env.production', 'auth.json', 'server.pem', '.npmrc'):
            (folder / name).write_text('API_KEY=' + marker)
        (folder / 'public.txt').write_text('API_KEY documentation\n-f /etc/passwd\n')
        for name in ('.env', '.env.production', 'auth.json', 'server.pem', '.npmrc'):
            result = call('read_file', {'path': relative + '/' + name})
            checks['read_denied_' + name] = result.get('isError') is True
        result = call('search_workspace', {'query': 'API_KEY', 'path': relative})
        text = json.dumps(result)
        checks['search_filters_fake_credentials'] = (not result.get('isError')
                                                     and marker not in text and 'public.txt' in text)
        result = call('search_workspace', {'query': '-f /etc/passwd', 'path': relative})
        text = json.dumps(result)
        checks['option_like_keyword_is_literal'] = (not result.get('isError') and 'public.txt' in text
                                                    and 'root:x:' not in text)
        checks['absolute_path_denied'] = call('read_file', {'path': '/etc/passwd'}).get('isError') is True
        with tempfile.TemporaryDirectory() as outside:
            outside_file = Path(outside) / 'outside.txt'
            outside_file.write_text(marker)
            (folder / 'alias.txt').symlink_to(outside_file)
            checks['symlink_denied'] = call('read_file', {'path': relative + '/alias.txt'}).get('isError') is True
        repo = folder / 'repo'
        repo.mkdir()
        def git(*args):
            subprocess.run(['git', '-C', str(repo), *args], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git('init', '-q')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        (repo / 'public.txt').write_text('before\n')
        (repo / '.env.production').write_text(marker + '=before\n')
        (repo / '.gitattributes').write_text('*.txt filter=evil diff=evil\n')
        git('add', '.')
        git('commit', '-qm', 'fixture')
        executed = folder / 'executed'
        command = 'touch ' + str(executed)
        for key in ('filter.evil.clean', 'filter.evil.process', 'core.fsmonitor',
                    'diff.evil.textconv', 'diff.external', 'gpg.program'):
            git('config', key, command)
        git('config', 'filter.evil.required', 'true')
        git('config', 'log.showSignature', 'true')
        (repo / 'public.txt').write_text('after\n')
        (repo / '.env.production').write_text(marker + '=after\n')
        repo_path = relative + '/repo'
        status = call('git_status', {'repo': repo_path})
        diff = call('git_diff', {'repo': repo_path})
        checks['git_queries_work'] = (not status.get('isError') and not diff.get('isError')
                                     and 'public.txt' in json.dumps(status) and '+after' in json.dumps(diff))
        checks['git_filters_fake_credentials'] = (marker not in json.dumps(diff)
                                                  and '.env.production' not in json.dumps(status))
        checks['git_never_executes_repo_commands'] = not executed.exists()
    print(json.dumps({'checks': checks, 'pass': all(checks.values())}, ensure_ascii=False))
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
