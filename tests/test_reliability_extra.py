"""补充并发回归：所有进程、系统任务和应用文件都使用隔离替身。"""

import copy
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

_sys_hook, _thread_hook = sys.excepthook, threading.excepthook
import PyTaskScheduler as app
sys.excepthook, threading.excepthook = _sys_hook, _thread_hook


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.pid = 123456

    def poll(self):
        return self.returncode

    def wait(self, *args, **kwargs):
        return self.returncode


class ReliabilityExtraTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, value in {
            "CONFIG_PATH": os.path.join(self.temp.name, "config.json"),
            "LOGS_DIR": self.temp.name,
            "RUNS_LOG_PATH": os.path.join(self.temp.name, "runs.jsonl"),
            "DAILY_LOG_DIR": os.path.join(self.temp.name, "daily"),
            "ERROR_LOG_PATH": os.path.join(self.temp.name, "errors.log"),
        }.items():
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("_exhook", "watchdog_set", "kill_tree"):
            patcher = patch.object(app, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(app, "watchdog_state", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(app, "process_identity", return_value=456, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def scheduler(self):
        task = copy.deepcopy(app.TASK_DEFAULTS)
        task.update(id="concurrent-test", name="并发测试", script="old.py",
                    console="window", enabled=True)
        scheduler = app.Scheduler({"tasks": [task], "settings": {}})
        scheduler.resolve_python = Mock(return_value=(r"D:\Python\python.exe", None))
        scheduler.script_full_path = Mock(return_value=("old.py", None))
        scheduler._watch = Mock()
        return scheduler, task

    def test_disabled_pending_launch_cannot_kill_previous_process(self):
        scheduler, task = self.scheduler()
        task["conflict"] = "kill_previous"
        previous = FakeProcess()
        scheduler.running[task["id"]] = previous
        scheduler._kill_and_confirm = Mock(return_value=True)
        entered, resume = threading.Event(), threading.Event()
        errors = []

        def resolve_script(_task):
            entered.set()
            if not resume.wait(5):
                raise RuntimeError("测试未释放路径解析")
            return "old.py", None

        def launch():
            try:
                scheduler.fire(task)
            except Exception as exc:
                errors.append(exc)

        scheduler.script_full_path = resolve_script
        window = SimpleNamespace(sched=scheduler, cfg=scheduler.cfg,
                                 _selected_task=Mock(return_value=task), _need_rebuild=False)
        with patch.object(app.os.path, "isfile", return_value=True), \
                patch.object(app.subprocess, "Popen", return_value=FakeProcess()) as popen:
            worker = threading.Thread(target=launch, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                app.App._toggle_task(window)
                self.assertFalse(task["enabled"])
            finally:
                resume.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            scheduler._kill_and_confirm.assert_not_called()
            self.assertFalse(getattr(previous, "_pytask_killed", False))
            popen.assert_not_called()

    def test_close_confirmation_freezes_new_async_launches(self):
        scheduler, task = self.scheduler()
        scheduler.running[task["id"]] = FakeProcess()
        other = dict(task, id="other-task")
        scheduler.cfg["tasks"].append(other)
        launch_results = []

        def answer(*args, **kwargs):
            launch_results.append(scheduler._fire_async(other, manual=True))
            return None

        window = SimpleNamespace(sched=scheduler, cfg=scheduler.cfg, destroy=Mock())
        with patch.object(app.messagebox, "askyesnocancel", side_effect=answer), \
                patch.object(app.subprocess, "Popen") as popen:
            app.App._on_close(window)
        self.assertEqual(launch_results, [False])
        self.assertFalse(scheduler._closing)
        self.assertFalse(scheduler.stop_event.is_set())
        window.destroy.assert_not_called()
        popen.assert_not_called()

    def test_close_save_failure_preserves_application_and_schedule(self):
        scheduler, _ = self.scheduler()
        window = SimpleNamespace(sched=scheduler, cfg=scheduler.cfg, destroy=Mock())
        with patch.object(app, "save_config", return_value=False), \
                patch.object(app.messagebox, "showerror") as error:
            app.App._on_close(window)
        window.destroy.assert_not_called()
        error.assert_called_once()
        self.assertFalse(scheduler.stop_event.is_set())
        self.assertFalse(scheduler._closing)

    def test_close_confirmation_does_not_consume_due_schedule(self):
        scheduler, task = self.scheduler()
        due = datetime.now() - timedelta(seconds=1)
        task.update(trigger_type="once", once_datetime=app.iso(due),
                    next_run=app.iso(due), finished=False)
        before = copy.deepcopy(task)
        scheduler.set_closing(True)
        try:
            with patch.object(app, "save_config") as save, \
                    patch.object(scheduler, "_fire_async") as launch:
                scheduler._tick()
            self.assertEqual(task, before)
            save.assert_not_called()
            launch.assert_not_called()
        finally:
            scheduler.set_closing(False)

    def test_settings_save_failure_keeps_dialog_and_marks_retry(self):
        scheduler, _ = self.scheduler()
        scheduler.mark_dirty = Mock()
        dialog = SimpleNamespace(
            master=SimpleNamespace(cfg=scheduler.cfg, sched=scheduler),
            var_py=Mock(get=Mock(return_value=sys.executable)),
            var_missed=Mock(get=Mock(return_value=app.MISSED_POLICY_LABELS["run_once"])),
            var_retain=Mock(get=Mock(return_value="7")),
            var_autostart=Mock(get=Mock(return_value=False)),
            var_watchdog=Mock(get=Mock(return_value=False)),
            ok=False, destroy=Mock(),
        )
        with patch.object(app, "save_config", return_value=False), \
                patch.object(app, "autostart_state", return_value=False), \
                patch.object(app, "autostart_set") as autostart, \
                patch.object(app.messagebox, "showerror") as error, \
                patch.object(app.messagebox, "showwarning"):
            app.SettingsDialog._on_save(dialog)
        dialog.destroy.assert_not_called()
        self.assertFalse(dialog.ok)
        scheduler.mark_dirty.assert_called()
        error.assert_called_once()
        autostart.assert_not_called()

    def test_cancelled_close_resumes_already_queued_launch(self):
        scheduler, task = self.scheduler()
        resolving, resume = threading.Event(), threading.Event()
        waiting, launched = threading.Event(), threading.Event()
        real_wait = scheduler._launch_condition.wait

        def resolve_script(_task):
            resolving.set()
            if not resume.wait(5):
                raise RuntimeError("测试未释放路径解析")
            return "old.py", None

        def wait(*args, **kwargs):
            waiting.set()
            return real_wait(*args, **kwargs)

        def popen(*args, **kwargs):
            launched.set()
            return FakeProcess()

        scheduler.script_full_path = resolve_script
        with patch.object(app.os.path, "isfile", return_value=True), \
                patch.object(app.subprocess, "Popen", side_effect=popen), \
                patch.object(scheduler._launch_condition, "wait", side_effect=wait):
            self.assertTrue(scheduler._fire_async(task, manual=True))
            try:
                self.assertTrue(resolving.wait(2))
                scheduler.set_closing(True)
                resume.set()
                self.assertTrue(waiting.wait(2))
                self.assertFalse(launched.is_set())
                scheduler.set_closing(False)
                self.assertTrue(launched.wait(2))
            finally:
                resume.set()
                scheduler.set_closing(False)
                with scheduler.lock:
                    workers = list(scheduler._workers)
                for worker in workers:
                    worker.join(3)


if __name__ == "__main__":
    unittest.main()
