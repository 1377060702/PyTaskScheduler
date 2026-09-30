import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

import PyTaskScheduler as app


class RuntimeRecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        for name, value in {"CONFIG_PATH": str(self.folder / "config.json"),
                            "LOGS_DIR": str(self.folder),
                            "DAILY_LOG_DIR": str(self.folder / "daily")}.items():
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_scheduler(self, active=None):
        task = dict(app.TASK_DEFAULTS, id="abc123abc123", script="task.py", console="window")
        if active:
            task["active_run"] = active
        scheduler = app.Scheduler({"settings": {}, "tasks": [task]})
        return scheduler, task

    def active(self):
        return {"pid": 123, "created": 456, "started": app.iso(datetime.now() - timedelta(seconds=20)),
                "planned": app.iso(datetime.now() - timedelta(seconds=20)), "missed": False,
                "task": dict(app.TASK_DEFAULTS, id="abc123abc123", timeout=60)}

    def test_launch_records_process_identity_before_returning(self):
        scheduler, task = self.make_scheduler()
        scheduler.resolve_python = Mock(return_value=("python.exe", None))
        scheduler.script_full_path = Mock(return_value=("task.py", None))
        proc = Mock(pid=123, poll=Mock(return_value=None))
        with patch.object(app, "process_identity", return_value=456, create=True), \
                patch.object(app.os.path, "isfile", return_value=True), \
                patch.object(app.subprocess, "Popen", return_value=proc), \
                patch.object(app.threading, "Thread"):
            self.assertTrue(scheduler.fire(task, manual=True))
        self.assertEqual(task.get("active_run", {}).get("created"), 456)
        self.assertEqual(task["active_run"]["pid"], 123)

    def test_restore_uses_pid_and_creation_time(self):
        active = self.active()
        scheduler, task = self.make_scheduler(active)
        restore = getattr(scheduler, "_restore_running", None)
        self.assertTrue(callable(restore), "必须恢复仍存活的任务实例")
        proc = Mock(pid=123, poll=Mock(return_value=None))
        with patch.object(app, "attach_process", return_value=proc, create=True) as attach, \
                patch.object(app.threading, "Thread") as thread:
            restore()
        attach.assert_called_once_with(123, 456)
        self.assertIs(scheduler.running[task["id"]], proc)
        thread.return_value.start.assert_called_once()

    def test_unknown_process_identity_disables_task_instead_of_starting_duplicate(self):
        scheduler, task = self.make_scheduler(self.active())
        restore = getattr(scheduler, "_restore_running", None)
        self.assertTrue(callable(restore))
        with patch.object(app, "attach_process", side_effect=PermissionError("拒绝访问"), create=True), \
                patch.object(app, "_exhook"):
            restore()
        self.assertFalse(task["enabled"])
        self.assertIsNotNone(task.get("active_run"))

    def test_unverified_runtime_identity_cannot_be_deleted_as_finished(self):
        scheduler, task = self.make_scheduler(self.active())
        task["enabled"] = False
        self.assertFalse(scheduler.cancel_task(task["id"]))
        self.assertIsNotNone(task.get("active_run"))
        self.assertIn(task, scheduler.cfg["tasks"])

    def test_watcher_only_clears_its_own_runtime_record(self):
        active = self.active()
        scheduler, task = self.make_scheduler(active)
        old = Mock(pid=77, wait=Mock(return_value=0), poll=Mock(return_value=0))
        old._pytask_started = datetime.now()
        old._pytask_killed = False
        scheduler.running[task["id"]] = Mock(pid=123, poll=Mock(return_value=None))
        scheduler.log_run = Mock()
        scheduler._watch(dict(task), old, None, datetime.now(), False)
        self.assertEqual(task["active_run"], active)

    def test_duplicate_task_does_not_inherit_process_identity_or_completion(self):
        scheduler, task = self.make_scheduler(self.active())
        task.update(finished=True, last_result="运行中…", last_run=app.iso(datetime.now()))
        window = SimpleNamespace(cfg=scheduler.cfg, sched=scheduler,
                                 wait_window=Mock(), _need_rebuild=False)
        with patch.object(app, "TaskDialog", return_value=SimpleNamespace(ok=True)):
            app.App._duplicate_task(window, task)
        duplicate = scheduler.cfg["tasks"][-1]
        self.assertNotIn("active_run", duplicate)
        self.assertFalse(duplicate["finished"])
        self.assertIsNone(duplicate["last_result"])

    def test_new_once_task_with_recent_past_time_uses_grace_without_restart(self):
        scheduler, task = self.make_scheduler()
        now = datetime.now()
        task.update(trigger_type="once", once_datetime=app.iso(now - timedelta(minutes=1)),
                    next_run=None, finished=False)
        scheduler._fire_async = Mock()
        scheduler._tick_one(task, now, now.timestamp(), 300, "skip", None)
        scheduler._fire_async.assert_called_once()
        self.assertTrue(task["finished"])


if __name__ == "__main__":
    unittest.main()
