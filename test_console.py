import io
import unittest
from unittest.mock import patch
import console as main


class ApprovalTests(unittest.TestCase):
    def test_noninteractive_plan_never_executes(self):
        commands = []
        def call(*args):
            commands.append(args)
            return 'Plan\n批准摘要 sha256：' + 'a' * 64
        with patch.object(main, 'state', return_value={'stage': 'planned'}), \
                patch.object(main, 'call', side_effect=call), \
                patch.object(main.sys, 'stdin', io.StringIO()), \
                patch('builtins.print'):
            main.proceed('a' * 12)
        self.assertEqual(commands, [('task', 'show-plan', 'a' * 12)])

    def test_declining_plan_never_executes(self):
        with patch.object(main, 'state', return_value={'stage': 'planned'}), \
                patch.object(main, 'call', return_value='Plan\n批准摘要 sha256：' + 'b' * 64) as call, \
                patch.object(main.sys.stdin, 'isatty', return_value=True), \
                patch('builtins.input', return_value='取消'), patch('builtins.print'):
            main.proceed('a' * 12)
        self.assertEqual(call.call_count, 1)

    def test_approval_binds_displayed_hash_before_execution(self):
        commands = []
        def call(*args):
            commands.append(args)
            return 'Plan\n批准摘要 sha256：' + 'c' * 64 if args[1] == 'show-plan' else ''
        with patch.object(main, 'state', side_effect=[{'stage': 'planned'}, {'stage': 'execution_failed'}, {'stage': 'execution_failed'}]), \
                patch.object(main, 'call', side_effect=call), \
                patch.object(main.sys.stdin, 'isatty', return_value=True), \
                patch('builtins.input', return_value='执行'), patch('builtins.print'):
            main.proceed('a' * 12)
        self.assertEqual(commands[1], ('task', 'approve', 'a' * 12, '--hash', 'c' * 64))
        self.assertEqual(commands[2], ('task', 'execute', 'a' * 12))


if __name__ == '__main__':
    unittest.main()
