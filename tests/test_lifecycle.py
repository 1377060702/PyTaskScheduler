"""生命周期回归：隔离 GUI、子进程、注册表和应用配置文件。"""

import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

_old_excepthook = sys.excepthook
_old_thread_excepthook = threading.excepthook
import PyTaskScheduler as app
sys.excepthook = _old_excepthook
threading.excepthook = _old_thread_excepthook


class FakeProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.pid = 123456
        self._pytask_killed = False

    def poll(self):
        return self.returncode

    def wait(self, *args, **kwargs):
        return self.returncode


class FakeTree:
    def __init__(self):
        self.rows = []

    def get_children(self, *args):
        return ()

    def delete(self, *args):
        self.rows.clear()

    def insert(self, parent, position, values, tags):
        self.rows.append(values)
        return str(len(self.rows))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patchers = []
        for name, value in {
            "CONFIG_PATH": os.path.join(self.temp.name, "config.json"),
            "LOGS_DIR": self.temp.name,
            "RUNS_LOG_PATH": os.path.join(self.temp.name, "runs.jsonl"),
            "DAILY_LOG_DIR": os.path.join(self.temp.name, "daily"),
            "ERROR_LOG_PATH": os.path.join(self.temp.name, "errors.log"),
        }.items():
            self.patchers.append(patch.object(app, name, value))
        self.patchers.extend([
            patch.object(app, "_exhook"),
            patch.object(app, "watchdog_state", return_value=False),
            patch.object(app, "watchdog_set"),
            patch.object(app, "kill_tree"),
            patch.object(app, "process_identity", return_value=456, create=True),
        ])
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_scheduler(self):
        task = copy.deepcopy(app.TASK_DEFAULTS)
        task.update(id="123456abcdef", name="生命周期测试", script="test.py",
                    console="window", enabled=True)
        cfg = {"tasks": [task], "settings": {}}
        scheduler = app.Scheduler(cfg)
        scheduler.resolve_python = Mock(return_value=(r"D:\Python\python.exe", None))
        scheduler.script_full_path = Mock(return_value=("test.py", None))
        scheduler._watch = Mock()
        return scheduler, task

    def test_cancel_close_keeps_application_and_scheduler_running(self):
        scheduler = Mock()
        scheduler.running = {"task": FakeProcess()}
        window = SimpleNamespace(sched=scheduler, cfg={"tasks": []}, destroy=Mock())
        with patch.object(app.messagebox, "askyesnocancel", return_value=None), \
                patch.object(app, "save_config") as save:
            app.App._on_close(window)
        window.destroy.assert_not_called()
        scheduler.stop.assert_not_called()
        save.assert_not_called()

    def test_delete_keeps_task_and_process_when_termination_fails(self):
        scheduler, task = self.make_scheduler()
        process = FakeProcess()
        scheduler.running[task["id"]] = process
        scheduler._kill_and_confirm = Mock(return_value=False)
        window = SimpleNamespace(
            sched=scheduler, cfg=scheduler.cfg,
            _selected_task=Mock(return_value=task), _is_running=Mock(return_value=True),
            _need_rebuild=False,
        )
        with patch.object(app.messagebox, "askyesno", return_value=True), \
                patch.object(app.messagebox, "showerror"), \
                patch.object(app.messagebox, "showwarning"), \
                patch.object(app, "save_config"):
            app.App._delete_task(window)
        self.assertIn(task, scheduler.cfg["tasks"])
        self.assertIs(scheduler.running.get(task["id"]), process)

    def test_stop_prevents_new_process_creation(self):
        scheduler, task = self.make_scheduler()
        scheduler.stop()
        with patch.object(app.os.path, "isfile", return_value=True), \
                patch.object(app.subprocess, "Popen", return_value=FakeProcess()) as popen:
            scheduler.fire(task, manual=True)
        popen.assert_not_called()

    def test_close_preserves_management_when_process_cannot_be_stopped(self):
        scheduler, task = self.make_scheduler()
        process = FakeProcess()
        scheduler.running[task["id"]] = process
        scheduler._kill_and_confirm = Mock(return_value=False)
        window = SimpleNamespace(sched=scheduler, cfg=scheduler.cfg, destroy=Mock())
        with patch.object(app.messagebox, "askyesnocancel", return_value=True), \
                patch.object(app.messagebox, "showerror"), \
                patch.object(app.messagebox, "showwarning"), \
                patch.object(app, "save_config"):
            app.App._on_close(window)
        window.destroy.assert_not_called()
        self.assertIs(scheduler.running.get(task["id"]), process)
        self.assertFalse(scheduler.stop_event.is_set(), "终止失败后窗口保留时必须恢复调度")

    def test_removed_task_cannot_start_from_stale_reference(self):
        scheduler, task = self.make_scheduler()
        scheduler.cfg["tasks"].remove(task)
        with patch.object(app.os.path, "isfile", return_value=True), \
                patch.object(app.subprocess, "Popen", return_value=FakeProcess()) as popen:
            scheduler.fire(task, manual=True)
        popen.assert_not_called()

    def test_cancellation_during_path_resolution_prevents_launch(self):
        scheduler, task = self.make_scheduler()
        cancel = getattr(scheduler, "cancel_task", None)
        self.assertTrue(callable(cancel), "Scheduler.cancel_task 必须覆盖已进入启动流程的任务")
        entered = threading.Event()
        resume = threading.Event()
        errors = []

        def resolve_script(_task):
            entered.set()
            if not resume.wait(5):
                raise RuntimeError("测试未释放路径解析")
            return "test.py", None

        def fire():
            try:
                scheduler.fire(task, manual=True)
            except Exception as exc:
                errors.append(exc)

        scheduler.script_full_path = resolve_script
        with patch.object(app.os.path, "isfile", return_value=True), \
                patch.object(app.subprocess, "Popen", return_value=FakeProcess()) as popen:
            worker = threading.Thread(target=fire, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(2), "启动线程必须进入路径解析")
                self.assertTrue(cancel(task["id"]))
            finally:
                resume.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            popen.assert_not_called()

    def test_old_watcher_cannot_overwrite_new_run_status(self):
        scheduler, task = self.make_scheduler()
        old_process = FakeProcess(returncode=0)
        new_process = FakeProcess()
        task.update(last_result="运行中…", last_run="2026-09-23 10:00:00")
        scheduler.running[task["id"]] = new_process
        scheduler.log_run = Mock()
        app.Scheduler._watch(scheduler, task, old_process, None, datetime.now(), False)
        self.assertEqual(task["last_result"], "运行中…")
        self.assertEqual(task["last_run"], "2026-09-23 10:00:00")
        self.assertIs(scheduler.running[task["id"]], new_process)
        scheduler.log_run.assert_called_once()

    def test_config_and_scheduler_share_mutation_lock(self):
        scheduler, _ = self.make_scheduler()
        self.assertIs(scheduler.lock, app.CFG_LOCK)

    def test_failed_atomic_replace_preserves_previous_config(self):
        with open(app.CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump({"tasks": ["previous"]}, handle)
        with patch.object(app.os, "replace", side_effect=OSError("模拟磁盘异常")):
            result = app.save_config({"tasks": ["new"]})
        with open(app.CONFIG_PATH, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"tasks": ["previous"]})
        self.assertIs(result, False, "落盘失败必须让调用方知道并保留重试标记")

    def test_successful_save_flushes_file_before_replace(self):
        calls = []
        real_replace = app.os.replace

        def replace(*args):
            calls.append("replace")
            return real_replace(*args)

        with patch.object(app.os, "fsync", side_effect=lambda fd: calls.append("fsync")), \
                patch.object(app.os, "replace", side_effect=replace):
            result = app.save_config({"tasks": [], "settings": {}})
        self.assertIs(result, True)
        self.assertEqual(calls, ["fsync", "replace"])

    def test_failed_background_save_is_retried(self):
        scheduler, _ = self.make_scheduler()
        scheduler._dirty = True
        scheduler.stop_event = Mock()
        scheduler.stop_event.wait.side_effect = [False, False, True]
        with patch.object(app, "save_config", side_effect=[False, True]) as save:
            scheduler._flush_loop()
        self.assertEqual(save.call_count, 2, "第一次落盘失败应保留_dirty并在下轮重试")

    def test_run_window_reads_latest_matching_rows(self):
        filename = os.path.join(self.temp.name, "runs-2026-09-23.jsonl")
        with open(filename, "w", encoding="utf-8") as handle:
            for sequence in range(7):
                record = {"task_name": "生命周期测试", "status": "success",
                          "status_cn": "成功", "time": str(sequence)}
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        window = SimpleNamespace(
            tree=FakeTree(), _row_meta={}, status=Mock(),
            var_task=Mock(get=Mock(return_value="全部")),
            var_status=Mock(get=Mock(return_value="全部")),
            var_range=Mock(get=Mock(return_value="全部")),
        )
        with patch.object(app, "MAX_RUN_ROWS", 3):
            app.RunsWindow._load(window)
        self.assertEqual([row[0] for row in window.tree.rows], ["6", "5", "4"])

    def test_cross_midnight_completion_does_not_reset_today_statistics(self):
        scheduler, task = self.make_scheduler()
        now = datetime.now()
        yesterday = now - timedelta(days=1)
        scheduler.day_stats.update(day=now.strftime("%Y-%m-%d"), success=2)
        scheduler.log_run(task, yesterday, yesterday, now, "success")
        self.assertEqual(scheduler.day_stats["day"], now.strftime("%Y-%m-%d"))
        self.assertGreaterEqual(scheduler.day_stats["success"], 2,
                                "昨日启动任务结束不能抹掉今天已有计数")


if __name__ == "__main__":
    unittest.main()
