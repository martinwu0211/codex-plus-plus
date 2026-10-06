import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import workflow
import argparse
import json
import importlib.util


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.global_state = tempfile.TemporaryDirectory()
        self.addCleanup(self.global_state.cleanup)
        state_patch = patch.object(workflow, 'STATE', Path(self.global_state.name))
        state_patch.start()
        self.addCleanup(state_patch.stop)

    def test_status_recovers_interrupted_execution_only_when_global_lease_is_free(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            workflow.save(path, {'stage':'executing','task':'fixture'})
            (path/'execution.started').touch()
            args=argparse.Namespace(command='status')
            with workflow.task_lock(workflow.STATE):
                workflow.handle(args,path)
                self.assertEqual(json.loads((path/'state.json').read_text())['stage'],'executing')
            workflow.handle(args,path)
            state=json.loads((path/'state.json').read_text())
            self.assertEqual(state['stage'],'execution_failed')
            self.assertEqual(state['acceptance'],'failed')
            self.assertTrue((path/'execution.started').exists())

    def test_start_generates_prompt_inside_existing_task_lock(self):
        import chat_task as chat
        for phase in ('plan', 'review'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root:
                path = Path(root) / 'task'
                path.mkdir()
                runtime = Path(root) / 'state'
                runtime.mkdir()
                (runtime / 'public_url').write_text('https://example.invalid')
                (runtime / 'mcp.secret').write_text('fake-test-secret')
                (runtime / 'connector_url').write_text('https://example.invalid/mcp/fake-test-secret')
                (runtime / 'connector.json').write_text('{"toolsVerified":true}')
                (path / 'plan.md').write_text('fixture plan')
                (path / 'execution.md').write_text('fixture execution')
                workflow.save(path, {'stage': 'new' if phase == 'plan' else 'executed',
                                     'task': 'fixture', 'executionHash': workflow.digest('fixture execution')})
                args = argparse.Namespace(command='start', phase=phase, task_id='a' * 12)
                with workflow.task_lock(path), patch.object(chat, 'STATE', runtime), \
                     patch.object(chat, 'ensure_browser'), patch.object(chat, 'browser') as browser, \
                     patch.object(chat.subprocess, 'run', side_effect=AssertionError('No nested workflow process')):
                    chat.handle(args, path)
                    browser.assert_any_call('task-submit', 'a' * 12, phase)
                self.assertIn('fixture', (path / (phase + '-prompt.txt')).read_text())

    def test_global_execution_lock_rejects_second_task_before_spawn(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            with workflow.task_lock(workflow.STATE), patch.object(workflow, '_execute') as execute:
                with self.assertRaisesRegex(ValueError, 'busy'):
                    workflow.execute(path, {})
                execute.assert_not_called()

    def test_browser_start_failure_cancels_queue_but_keeps_uncertain_submission(self):
        import chat_task as chat
        for uncertain, owned, cleanup_fails in ((False,True,False),(False,False,False),(False,True,True),(True,True,False)):
            with self.subTest(uncertain=uncertain,owned=owned,cleanup_fails=cleanup_fails), tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'task'; path.mkdir()
                runtime=Path(directory)/'state'; runtime.mkdir()
                for name,text in {'public_url':'https://example.invalid', 'mcp.secret':'fake-secret',
                                  'connector_url':'https://example.invalid/mcp/fake-secret',
                                  'connector.json':'{"toolsVerified":true}'}.items():
                    (runtime/name).write_text(text)
                workflow.save(path, {'task':'fixture','stage':'new'})
                def submit(*args):
                    if args[0]=='task-submit':
                        (path/'plan-submitted.json').write_text('{"sent":false}')
                        raise RuntimeError('Submission uncertain')
                args=argparse.Namespace(command='start',phase='plan',task_id='a'*12)
                def ensure(on_start):
                    if owned:
                        on_start()
                    if not uncertain:
                        raise RuntimeError('Queue timeout')
                with patch.object(chat,'STATE',runtime), patch.object(chat,'browser',side_effect=submit), \
                     patch.object(chat,'ensure_browser',side_effect=ensure), \
                     patch.object(chat.subprocess,'run',side_effect=chat.subprocess.TimeoutExpired('stop',65) if cleanup_fails else None) as stop:
                    with self.assertRaises(RuntimeError):
                        chat.handle(args,path)
                    if uncertain:
                        stop.assert_not_called()
                        self.assertTrue((runtime/'chat-active.json').exists())
                    else:
                        if owned:
                            self.assertEqual(stop.call_args.args[0][-1],'stop')
                        else:
                            stop.assert_not_called()
                        self.assertFalse((runtime/'chat-active.json').exists())

    def test_changed_plan_cannot_run(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "plan.md").write_text("approved task")
            state = {"stage": "planned", "task": "fixture"}
            workflow.approve(path, state, workflow.approval_hash(state, 'approved task'))
            (path / "plan.md").write_text("different task")
            with patch.object(workflow.subprocess, "Popen") as run:
                with self.assertRaises(ValueError):
                    workflow.execute(path, state)
                run.assert_not_called()

    def test_started_execution_cannot_run_again(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "plan.md").write_text("task")
            state = {"stage": "executing", "task": "fixture", "approvedPlanHash": workflow.digest("task")}
            with patch.object(workflow.subprocess, "Popen") as run:
                with self.assertRaises(ValueError):
                    workflow.execute(path, state)
                run.assert_not_called()

    def test_approval_requires_reviewed_task_and_plan_hash(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / 'plan.md').write_text('plan')
            state = {'stage': 'planned', 'task': 'first task'}
            shown = workflow.approval_hash(state, 'plan')
            state['task'] = 'replacement task'
            with self.assertRaises(ValueError):
                workflow.approve(path, state, shown)

    def test_task_lock_rejects_concurrent_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            with workflow.task_lock(path):
                with self.assertRaisesRegex(ValueError, 'busy'):
                    with workflow.task_lock(path):
                        self.fail('Concurrent task lock acquired')

    def test_atomic_execution_token_rejects_repeat_before_spawning(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / 'plan.md').write_text('plan')
            (path / 'execution.started').touch()
            state = {'stage': 'planned', 'task': 'fixture'}
            workflow.approve(path, state, workflow.approval_hash(state, 'plan'))
            with patch.object(workflow.subprocess, 'Popen') as spawn:
                with self.assertRaises(FileExistsError):
                    workflow.execute(path, state)
                spawn.assert_not_called()

    def test_poll_preserves_approved_plan_and_never_calls_browser(self):
        spec = importlib.util.spec_from_file_location('chat_task_test', Path(__file__).with_name('chat_task.py'))
        chat = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(chat)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / 'plan.md').write_text('approved plan')
            (path / 'state.json').write_text(json.dumps({'stage': 'approved'}))
            args = argparse.Namespace(command='poll', phase='plan', task_id='a' * 12)
            with patch.object(chat, 'browser') as action:
                chat.handle(args, path)
                action.assert_not_called()
            self.assertEqual((path / 'plan.md').read_text(), 'approved plan')

    def test_accept_rejects_modified_review_and_repeat_acceptance(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / 'review.md').write_text('modified review')
            (path / 'review-collected.json').write_text(json.dumps({'responseHash': workflow.digest('real review')}))
            state = {'stage': 'reviewed', 'acceptance': 'pending-review'}
            workflow.save(path, state)
            args = argparse.Namespace(command='accept', value='a' * 12)
            with self.assertRaisesRegex(ValueError, 'differs'):
                workflow.handle(args, path)
            state['acceptance'] = 'accepted'
            workflow.save(path, state)
            with self.assertRaises(ValueError):
                workflow.handle(args, path)

    def test_timeout_cleans_process_and_records_failed_state(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / 'plan.md').write_text('plan')
            state = {'stage': 'planned', 'task': 'fixture'}
            workflow.approve(path, state, workflow.approval_hash(state, 'plan'))
            with patch.object(workflow.subprocess, 'Popen') as spawn, patch.object(workflow, 'stop_execution_group') as stop:
                spawn.return_value.communicate.side_effect = workflow.subprocess.TimeoutExpired('fixture', 1)
                spawn.return_value.returncode = -15
                with self.assertRaises(workflow.subprocess.TimeoutExpired):
                    workflow.execute(path, state)
                stop.assert_called_once_with(spawn.return_value)
            self.assertEqual(json.loads((path / 'state.json').read_text())['acceptance'], 'failed')
