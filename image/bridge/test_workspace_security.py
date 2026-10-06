"""Exercise real credential filtering and malicious Git config with fake data."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


class WorkspaceBoundary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'workspace'
        self.root.mkdir()
        secret = self.base / 'fake.secret'
        secret.write_text('fake-test-credential-with-at-least-32-characters')
        self.secret = secret
        self.bridge = self.load_bridge()

    def load_bridge(self, deny_names=''):
        env = {'MCP_WORKSPACE': str(self.root), 'MCP_SECRET_FILE': str(self.secret),
               'MCP_DENY_NAMES': deny_names}
        with mock.patch.dict(os.environ, env):
            spec = importlib.util.spec_from_file_location('bridge_test', Path(__file__).with_name('server.py'))
            bridge = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bridge)
            return bridge

    def test_custom_names_filter_all_workspace_queries_without_disabling_defaults(self):
        folder = self.root / 'restricted-content'
        folder.mkdir()
        hidden = folder / 'notes.txt'
        hidden.write_text('CUSTOM_MARKER before\n')
        named = self.root / 'notes.txt'
        named.write_text('CUSTOM_MARKER before\n')
        public = self.root / 'public.txt'
        public.write_text('CUSTOM_MARKER before\n')
        self.assertIn('CUSTOM_MARKER', self.bridge.t_read_file({'path': 'restricted-content/notes.txt'}))
        for args in (('init', '-q'), ('config', 'user.name', 'Test'),
                     ('config', 'user.email', 'test@example.invalid'),
                     ('add', '.'), ('commit', '-qm', 'fixture')):
            subprocess.run(['git', '-C', str(self.root), *args], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        hidden.write_text('CUSTOM_MARKER PRIVATE after\n')
        named.write_text('CUSTOM_MARKER PRIVATE after\n')
        public.write_text('CUSTOM_MARKER PUBLIC after\n')
        self.bridge = self.load_bridge(' Restricted-Content, NOTES.txt, ,')
        for path in ('restricted-content/notes.txt', 'notes.txt', 'credentials/info.txt', '.env.local'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.bridge.t_read_file({'path': path})
        listing = self.bridge.t_list_directory({})
        search = self.bridge.t_search_workspace({'query': 'CUSTOM_MARKER'})
        status = self.bridge.t_git_status({'repo': '.'})
        diff = self.bridge.t_git_diff({'repo': '.'})
        for output in (listing, search, status, diff):
            self.assertNotIn('restricted-content', output)
            self.assertNotIn('notes.txt', output)
            self.assertIn('public.txt', output)
        self.assertNotIn('PRIVATE after', diff)
        self.assertIn('PUBLIC after', diff)

    def test_custom_names_reject_paths_and_traversal(self):
        for value in ('.', '..', '../private', 'blocked/file', 'blocked\\file'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'MCP_DENY_NAMES'):
                self.load_bridge(value)

    def test_sensitive_variants_are_blocked_by_read_list_and_search(self):
        names = ('.env', '.env.local', '.env.production', 'auth.json', 'server.pem',
                 'id_rsa', '.npmrc', '.git-credentials', 'private.key', '.envrc',
                 'prod.env', 'mcp.secret', 'token.json', 'state.tfstate', '.claude.json',
                 'tokens.json', 'session.token', 'backup.gpg', 'session.sqlite',
                 'Login Data', 'Cookies', 'rclone.conf', 'client_secret.json',
                 'serviceAccountKey.json', 'id_ed25519_sk', 'id_ecdsa_sk',
                 'env.local', 'kubeconfig.yaml', 'private.asc', 'vpn.ovpn', 'Web Data', 'Local State')
        for name in names:
            (self.root / name).write_text('API_KEY=FAKE_SENSITIVE_VALUE')
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.bridge.t_read_file({'path': name})
        (self.root / 'public.txt').write_text('API_KEY documentation only')
        for number in range(3):
            (self.root / f'public{number}.txt').write_text('API_KEY documentation only')
        for name in ('secrets', 'credentials', 'User Data'):
            (self.root / name).mkdir()
            (self.root / name / 'normal.txt').write_text('API_KEY=FAKE_SENSITIVE_VALUE')
            with self.subTest(directory=name), self.assertRaises(ValueError):
                self.bridge.t_read_file({'path': name + '/normal.txt'})
        output = self.bridge.t_search_workspace({'query': 'API_KEY'})
        self.assertIn('public.txt', output)
        self.assertNotIn('FAKE_SENSITIVE_VALUE', output)
        listing = self.bridge.t_list_directory({})
        for name in names:
            self.assertNotIn(name, listing)

    def test_option_like_keywords_never_spawn_grep(self):
        (self.root / 'safe.txt').write_text('-f /etc/passwd\n--include=*.env\n')
        with mock.patch.object(self.bridge.subprocess, 'run', side_effect=AssertionError('No subprocess')):
            self.assertIn('safe.txt', self.bridge.t_search_workspace({'query': '-f /etc/passwd'}))
            self.assertIn('safe.txt', self.bridge.t_search_workspace({'query': '--include=*.env'}))

    def test_symbolic_links_and_traversal_are_not_exposed(self):
        outside = self.base / 'outside.txt'
        outside.write_text('OUTSIDE_SECRET')
        (self.root / 'alias.txt').symlink_to(outside)
        (self.root / 'aliasdir').symlink_to(self.base, target_is_directory=True)
        for path in ('alias.txt', 'aliasdir/outside.txt', '../outside.txt', str(outside)):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.bridge.t_read_file({'path': path})
        self.assertNotIn('OUTSIDE_SECRET', self.bridge.t_search_workspace({'query': 'OUTSIDE_SECRET'}))

    def test_fd_open_blocks_a_symlink_replaced_after_path_check(self):
        path = self.root / 'public.txt'
        path.write_text('public')
        checked = self.bridge.safe('public.txt')
        outside = self.base / 'outside.txt'
        outside.write_text('OUTSIDE_SECRET')
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(OSError):
            self.bridge.read_bytes(checked)

    def test_hard_links_to_external_files_are_rejected(self):
        outside = self.base / 'outside'
        outside.write_text('OUTSIDE_SECRET')
        os.link(outside, self.root / 'alias.txt')
        with self.assertRaises(ValueError):
            self.bridge.t_read_file({'path': 'alias.txt'})
        self.assertNotIn('alias.txt', self.bridge.t_list_directory({}))
        self.assertNotIn('OUTSIDE_SECRET', self.bridge.t_search_workspace({'query': 'OUTSIDE_SECRET'}))

    def test_git_never_runs_fsmonitor_textconv_or_external_diff_and_filters_secrets(self):
        repo = self.root / 'repo'
        repo.mkdir()
        def git(*args):
            subprocess.run(['git', '-C', str(repo), *args], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git('init', '-q')
        git('config', 'user.name', 'Test')
        git('config', 'user.email', 'test@example.invalid')
        (repo / 'public.txt').write_text('before\n')
        (repo / '.env.production').write_text('FAKE_SENSITIVE_VALUE=before\n')
        (repo / '.gitattributes').write_text('*.txt diff=evil filter=evil\n')
        git('add', '.')
        git('commit', '-qm', 'fixture')
        marker = self.base / 'command_was_run'
        script = self.base / 'evil.sh'
        script.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
        script.chmod(0o700)
        git('config', 'core.fsmonitor', str(script))
        git('config', 'diff.external', str(script))
        git('config', 'diff.evil.textconv', str(script))
        git('config', 'filter.evil.clean', str(script))
        git('config', 'filter.evil.process', str(script))
        git('config', 'filter.evil.required', 'true')
        git('config', 'gpg.program', str(script))
        git('config', 'log.showSignature', 'true')
        (repo / 'public.txt').write_text('after\n')
        (repo / '.env.production').write_text('FAKE_SENSITIVE_VALUE=after\n')
        status = self.bridge.t_git_status({'repo': 'repo'})
        diff = self.bridge.t_git_diff({'repo': 'repo'})
        self.assertIn('public.txt', status)
        self.assertNotIn('.env.production', status)
        self.assertIn('+after', diff)
        self.assertNotIn('FAKE_SENSITIVE_VALUE', diff)
        self.assertNotIn('.env.production', diff)
        self.assertIn('fixture', self.bridge.git('log', '-1', '--oneline', repo='repo'))
        self.assertFalse(marker.exists())
        # A config change during snapshot use cannot affect the command.
        with self.bridge.git_snapshot(repo) as snapshot:
            (repo / '.git' / 'config').write_text('malformed attacker-controlled config')
            self.assertEqual(self.bridge.git_run(snapshot, ['status', '--porcelain']).returncode, 0)

    def test_git_metadata_links_are_rejected(self):
        repo = self.root / 'repo'
        repo.mkdir()
        subprocess.run(['git', '-C', str(repo), 'init', '-q'], check=True)
        (repo / '.git' / 'HEAD').unlink()
        (repo / '.git' / 'HEAD').symlink_to(self.base / 'outside')
        with self.assertRaises(OSError):
            self.bridge.t_git_status({'repo': 'repo'})

    def test_git_submodules_never_run_their_own_configuration(self):
        repo = self.root / 'repo'
        sub = repo / 'sub'
        sub.mkdir(parents=True)
        def git(directory, *args):
            return subprocess.check_output(['git', '-C', str(directory), *args], stderr=subprocess.DEVNULL).decode().strip()
        for directory in (repo, sub):
            git(directory, 'init', '-q')
            git(directory, 'config', 'user.name', 'Test')
            git(directory, 'config', 'user.email', 'test@example.invalid')
        (sub / 'public.txt').write_text('before')
        git(sub, 'add', '.')
        git(sub, 'commit', '-qm', 'sub fixture')
        commit = git(sub, 'rev-parse', 'HEAD')
        git(repo, 'update-index', '--add', '--cacheinfo', '160000,' + commit + ',sub')
        git(repo, 'commit', '-qm', 'parent fixture')
        marker = self.base / 'submodule_command_was_run'
        script = self.base / 'submodule_evil.sh'
        script.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
        script.chmod(0o700)
        git(sub, 'config', 'core.fsmonitor', str(script))
        git(sub, 'config', 'filter.evil.clean', str(script))
        (sub / 'public.txt').write_text('after')
        self.bridge.t_git_status({'repo': 'repo'})
        self.bridge.t_git_diff({'repo': 'repo'})
        self.assertFalse(marker.exists(), 'Read-only queries executed submodule configuration')

    def test_git_diff_rejects_hard_links_and_uses_a_private_worktree(self):
        repo = self.root / 'repo'
        repo.mkdir()
        def git(*args):
            subprocess.run(['git', '-C', str(repo), *args], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git('init', '-q')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        public = repo / 'public.txt'
        public.write_text('before\n')
        git('add', '.')
        git('commit', '-qm', 'fixture')
        outside = self.base / 'outside'
        outside.write_text('OUTSIDE_SECRET\n')
        public.unlink()
        os.link(outside, public)
        self.assertNotIn('OUTSIDE_SECRET', self.bridge.t_git_diff({'repo': 'repo'}))
        public.unlink()
        public.write_text('after\n')
        original_run = self.bridge.git_run
        def replaced_after_snapshot(snapshot, args):
            if '--name-only' not in args:
                public.unlink()
                public.symlink_to(outside)
            return original_run(snapshot, args)
        with mock.patch.object(self.bridge, 'git_run', replaced_after_snapshot):
            output = self.bridge.t_git_diff({'repo': 'repo'})
        self.assertIn('+after', output)
        self.assertNotIn('OUTSIDE_SECRET', output)


if __name__ == '__main__':
    unittest.main()
