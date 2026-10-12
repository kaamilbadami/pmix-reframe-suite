"""Exercise real controller callbacks/workers without starting PRRTE."""
import ast
from collections import deque
from pathlib import Path
import subprocess
import threading
import time
import types
from unittest.mock import patch
import unittest

from pmix_python_binding.common.pmix_event_utils import (
    JobCompletionRouter, wait_for_job_completions,
    get_pmix_info_value, format_pmix_job_term_status,
)

CONTROLLERS = Path(__file__).resolve().parents[1] / 'controllers'
NAMES = ('worker_threads', 'targeted', 'mixed_thread')
INFO = [{'key': 'term', 'value': 0}]


def controller_functions(name):
    tree = ast.parse((CONTROLLERS / ('run_pmix_python_' + name + '_compat.py')).read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    ns = dict(
        threading=threading, time=time,
        pmix=types.SimpleNamespace(PMIX_SUCCESS=0, PMIX_JOB_TERM_STATUS='term',
            PMIX_EVENT_ACTION_COMPLETE=101, PMIX_MAPBY='map', PMIX_BINDTO='bind',
            PMIX_NOTIFY_COMPLETION='notify', PMIX_STRING=1, PMIX_BOOL=2,
            PMIX_HOST='host', PMIX_RANK_WILDCARD=-1,
            PMIX_FWD_STDOUT_CHANNEL=1, PMIX_FWD_STDERR_CHANNEL=2),
        get_pmix_info_value=get_pmix_info_value,
        format_pmix_job_term_status=format_pmix_job_term_status,
        running={}, active_jobs={}, host_q=deque(['remote']),
        run_var=threading.Condition(), done_var=threading.Condition(),
        active=0, avail=0, completed=0, num_iters=1, errors={},
        print_info=lambda *a, **kw: None, sleeper='payload',
        args=types.SimpleNamespace(delay=0, target_host_override=None),
        used_worker_ids=set(), used_worker_ids_lock=threading.Lock(),
        handlers={},
    )
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<controller>', 'exec'), ns)
    ns['print_info'] = lambda *a, **kw: None
    ns['debug'] = False
    ns['completion_router'] = JobCompletionRouter(ns['run_var'], ns['handle_job_completion'])
    return ns


class ControllerCompletionTests(unittest.TestCase):
    def run_worker(self, name, early=True, term=0, spawn_rc=0):
        ns = controller_functions(name)
        app = {'maxprocs': 2, 'my_id': 0}
        class OneJob:
            def __init__(self):
                self.used = False
            def get(self):
                if self.used:
                    raise StopIteration
                self.used = True
                return app
        jobs = OneJob()
        ns['run_queue'] = jobs
        ns['worker_queues'] = [jobs]
        info = [{'key': 'term', 'value': term}]
        def spawn(*args):
            if early and spawn_rc == 0:
                ns['next_handler'](0, 0, {'nspace': 'child', 'rank': 0}, info, [])
            return spawn_rc, 'child'
        ns['tool'] = types.SimpleNamespace(spawn=spawn, iof_pull=lambda *a: (0, 0),
                                          error_string=lambda rc: 'spawn failed')
        with self.assertRaises(StopIteration):
            ns['worker'](*([0] if name == 'worker_threads' else []))
        if not early and spawn_rc == 0:
            ns['next_handler'](0, 0, {'nspace': 'child', 'rank': 0}, info, [])
        return ns

    def test_early_completion_during_spawn_all_controllers(self):
        for name in NAMES:
            with self.subTest(controller=name):
                ns = self.run_worker(name)
                self.assertEqual((ns['completed'], ns['active'], ns['avail']), (1, 0, 2))
                self.assertEqual(ns['active_jobs'], {})
                if name == 'targeted':
                    self.assertEqual(list(ns['host_q']), ['remote'])

    def test_normal_completion_all_controllers(self):
        for name in NAMES:
            with self.subTest(controller=name):
                ns = self.run_worker(name, early=False)
                self.assertEqual((ns['completed'], ns['active'], ns['avail']), (1, 0, 2))

    def test_duplicate_completion_does_not_release_slots_twice(self):
        for name in NAMES:
            with self.subTest(controller=name):
                ns = self.run_worker(name)
                ns['next_handler'](0, 0, {'nspace': 'child', 'rank': 0}, INFO, [])
                self.assertEqual((ns['completed'], ns['active'], ns['avail']), (1, 0, 2))

    def test_failed_child_preserves_nonzero_status(self):
        for name in NAMES:
            with self.subTest(controller=name):
                ns = self.run_worker(name, term=7)
                self.assertTrue(any('PMIX_JOB_TERM_STATUS=7' in key for key in ns['errors']))
                self.assertEqual(ns['completed'], 1)

    def test_failed_spawn_restores_slots(self):
        for name in NAMES:
            with self.subTest(controller=name):
                ns = self.run_worker(name, spawn_rc=-1)
                self.assertEqual((ns['completed'], ns['active'], ns['avail']), (1, 0, 2))
                self.assertIn('spawn failed', ns['errors'])

    def test_missing_child_status_fails_all_controllers(self):
        for name in NAMES:
            with self.subTest(controller=name):
                ns = controller_functions(name)
                ns.update(active=2)
                ns['running']['child'] = {'maxprocs': 2, 'my_id': 0}
                ns['active_jobs']['child'] = 'remote'
                ns['completion_router'].register('child')
                ns['next_handler'](0, 0, {'nspace': 'child', 'rank': 0}, [], [])
                self.assertIn('missing PMIX_JOB_TERM_STATUS', ns['errors'])
                self.assertEqual(ns['completed'], 1)

    def test_concurrent_jobs_preserve_completion_count_and_slots(self):
        for name in NAMES:
            with self.subTest(controller=name):
                ns = controller_functions(name)
                ns.update(active=8, num_iters=8)
                for number in range(8):
                    key = str(number)
                    ns['running'][key] = {'maxprocs': 1, 'my_id': number}
                    ns['active_jobs'][key] = 'remote'
                    ns['completion_router'].register(key)
                threads = [threading.Thread(target=ns['next_handler'],
                    args=(0, 0, {'nspace': str(i), 'rank': 0}, INFO, []))
                    for i in range(8)]
                for thread in threads: thread.start()
                for thread in threads: thread.join()
                self.assertEqual((ns['completed'], ns['active'], ns['avail']), (8, 0, 8))

    def test_shutdown_timeout_is_reported_as_failure(self):
        for name in NAMES:
            with self.subTest(controller=name):
                ns = controller_functions(name)
                ns.update(subprocess=subprocess, Path=Path,
                          os=types.SimpleNamespace(environ={'PRRTE': '/fixture'}),
                          dvm_file='/tmp/test.uri')
                with patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('pterm', 20)):
                    self.assertFalse(ns['shutdown_dvm']())

    def test_unrelated_event_does_not_complete_known_job(self):
        ns = controller_functions('targeted')
        ns['completion_router'].record({'nspace': 'other', 'rank': 0}, INFO)
        ns['running']['child'] = {'maxprocs': 1, 'my_id': 0}
        ns['completion_router'].register('child')
        self.assertEqual(ns['completed'], 0)

    def test_queued_work_dispatches_without_a_prior_notification(self):
        for name in NAMES:
            with self.subTest(controller=name):
                ns = controller_functions(name)
                ns.update(fill_queue=[{'maxprocs': 1, 'my_id': 0}], avail=1, n_threads=1)
                delivered = []
                class Dispatch:
                    def put(self, app):
                        delivered.append(app)
                        raise StopIteration
                ns['run_queue'] = Dispatch()
                ns['worker_queues'] = [Dispatch()]
                # Fail promptly if the queuer regresses to unconditional wait.
                class Condition:
                    def __enter__(self): return self
                    def __exit__(self, *args): pass
                    def wait_for(self, predicate):
                        assert predicate(), 'runnable task was missed'
                    def wait(self):
                        raise AssertionError('unconditional wait')
                ns['run_var'] = Condition()
                with self.assertRaises(StopIteration):
                    ns['queuer']()
                self.assertEqual(len(delivered), 1)
                self.assertEqual(ns['avail'], 0)


class CompletionWaitTests(unittest.TestCase):
    def test_completion_before_wait_returns_immediately(self):
        self.assertTrue(wait_for_job_completions(threading.Condition(), lambda: 2, 2, 0))

    def test_incomplete_work_times_out(self):
        self.assertFalse(wait_for_job_completions(threading.Condition(), lambda: 1, 2, 0.01))

    def test_spurious_notification_does_not_report_success(self):
        cv = threading.Condition()
        def notify():
            time.sleep(0.01)
            with cv: cv.notify_all()
        thread = threading.Thread(target=notify)
        thread.start()
        try:
            self.assertFalse(wait_for_job_completions(cv, lambda: 0, 1, 0.03))
        finally:
            thread.join()

    def test_real_notification_releases_waiter(self):
        cv = threading.Condition()
        completed = [0]
        def finish():
            with cv:
                completed[0] = 1
                cv.notify_all()
        thread = threading.Thread(target=finish)
        thread.start()
        try:
            self.assertTrue(wait_for_job_completions(cv, lambda: completed[0], 1, 1))
        finally:
            thread.join()

    def test_early_event_is_copied_before_callback_data_changes(self):
        events = []
        router = JobCompletionRouter(threading.Condition(), lambda s, i: events.append((s, i)))
        source, info = {'nspace': 'child', 'rank': 0}, [{'key': 'term', 'value': 7}]
        router.record(source, info)
        source['rank'] = 9
        info[0]['value'] = 0
        router.register('child')
        self.assertEqual(events, [({'nspace': 'child', 'rank': 0}, [{'key': 'term', 'value': 7}])])

    def test_concurrent_duplicate_notifications_deliver_once(self):
        events = []
        router = JobCompletionRouter(threading.Condition(), lambda s, i: events.append(s))
        router.register('child')
        threads = [threading.Thread(target=router.record,
                   args=({'nspace': 'child', 'rank': 0}, INFO)) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(events), 1)
