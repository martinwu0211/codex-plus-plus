"""Lease and cleanup regression without a real browser or login profile."""
import fcntl
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location('browser', Path(__file__).with_name('browser.py'))
browser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(browser)


class BrowserLeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lock = self.root / 'host.lock'
        self.lock.touch()
        for name, value in {'STATE': self.root, 'PROFILE': self.root / 'profile',
                            'RUNTIME': self.root / 'browser.json'}.items():
            p = patch.object(browser, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'CODEXPP_CHROME_LOCK': str(self.lock),
                                    'CODEXPP_EXTERNAL_DISPLAY': '1',
                                    'CODEXPP_BROWSER_LIFETIME': '0'})
        p.start()
        self.addCleanup(p.stop)

    def test_reused_pid_is_not_stopped(self):
        browser.RUNTIME.write_text('{"pid":1,"start_ticks":"not-the-current-process","state":"running"}')
        self.assertEqual(browser.current(), {'state': 'stopped'})

    def test_cancel_while_queued_never_launches(self):
        with self.lock.open('r+') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            handlers = {}
            with patch.object(browser.signal, 'signal', lambda sig, fn: handlers.update({sig: fn})), \
                 patch.object(browser.time, 'sleep', lambda _: handlers[browser.signal.SIGTERM]()), \
                 patch.object(browser.subprocess, 'Popen') as launch:
                browser.serve('about:blank')
            launch.assert_not_called()
            self.assertFalse(browser.RUNTIME.exists())

    def test_lease_is_held_until_every_child_is_reaped(self):
        children = []
        def launch(*args, **kwargs):
            self.assertTrue(kwargs['pass_fds'])
            child = MagicMock()
            child.pid = 99999999
            child.poll.return_value = None
            def reaped():
                with self.lock.open('r+') as contender:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return 0
            child.wait.side_effect = reaped
            children.append(child)
            return child
        response = MagicMock()
        response.__enter__.return_value.status = 200
        with patch.object(browser.signal, 'signal'), \
             patch.object(browser.socket, 'socket'), \
             patch.object(browser.subprocess, 'Popen', side_effect=launch), \
             patch.object(browser.urllib.request, 'build_opener') as opener, \
             patch.object(browser, 'group_alive', return_value=False):
            opener.return_value.open.return_value = response
            browser.serve('about:blank')
        self.assertEqual(len(children), 3)
        for child in children:
            child.wait.assert_called_once()
        with self.lock.open('r+') as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertFalse(browser.RUNTIME.exists())
        self.assertEqual(browser.PROFILE.stat().st_mode & 0o777, 0o700)


if __name__ == '__main__':
    unittest.main()
