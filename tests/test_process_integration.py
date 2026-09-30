"""Windows 真实进程集成：仅运行临时脚本，配置、日志和清理目标完全隔离。"""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

_sys_hook, _thread_hook = sys.excepthook, threading.excepthook
import PyTaskScheduler as app
sys.excepthook, threading.excepthook = _sys_hook, _thread_hook


@unittest.skipUnless(os.name == "nt", "此项目的进程运行方式要求 Windows")
class ProcessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pytask-integration-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.marker = self.directory / "markers.jsonl"
        self.script = self.directory / "isolated_worker.py"
        self.script.write_text(
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            "with Path(sys.argv[1]).open('a', encoding='utf-8') as handle:\n"
            "    handle.write(json.dumps({'pid': os.getpid(), 'args': sys.argv[3:]}) + '\\n')\n"
            "    handle.flush()\n"
            "time.sleep(float(sys.argv[2]))\n"
            "print('isolated task completed', flush=True)\n",
            encoding="utf-8",
        )
        for name, value in {
            "CONFIG_PATH": str(self.directory / "config.json"),
            "LOGS_DIR": str(self.directory),
            "RUNS_LOG_PATH": str(self.directory / "runs.jsonl"),
            "DAILY_LOG_DIR": str(self.directory / "daily"),
            "ERROR_LOG_PATH": str(self.directory / "errors.log"),
        }.items():
            self.start_patch(patch.object(app, name, value))
        self.start_patch(patch.object(app, "watchdog_state", return_value=False))
        self.start_patch(patch.object(app, "watchdog_set"))
        self.start_patch(patch.object(app, "autostart_state", return_value=False))
        self.start_patch(patch.object(app, "autostart_set"))

        self.processes = []
        self.watch_done = {}
        real_popen = subprocess.Popen

        def start_process(command, *args, **kwargs):
            process = real_popen(command, *args, **kwargs)
            # subprocess.run(taskkill) 也经过 Popen，只登记本测试 Python 脚本的进程。
            if (isinstance(command, (list, tuple)) and len(command) > 1
                    and os.path.normcase(str(command[0])) == os.path.normcase(sys.executable)
                    and Path(command[1]) == self.script):
                self.processes.append(process)
                self.watch_done[process.pid] = threading.Event()
            return process

        self.start_patch(patch.object(app.subprocess, "Popen", side_effect=start_process))
        self.task = copy.deepcopy(app.TASK_DEFAULTS)
        self.task.update(
            id="isolated-process-test", name="隔离进程测试", enabled=True,
            script=str(self.script), workdir=str(self.directory), console="nowindow",
        )
        self.scheduler = app.Scheduler({
            "tasks": [self.task], "settings": {"python_dir": sys.executable},
        })
        real_watch = self.scheduler._watch

        def watch(task, process, *args):
            try:
                return real_watch(task, process, *args)
            finally:
                self.watch_done[process.pid].set()

        self.scheduler._watch = watch
        # 清理优先于解除 patch 和删除临时目录，避免后台 watcher 写回真实路径。
        self.addCleanup(self.cleanup_processes)

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def cleanup_processes(self):
        self.scheduler.stop()
        for process in self.processes:
            if process.poll() is None:
                # 临时脚本不创建后代；只终止记录在案、本测试创建的 Popen 对象。
                process.kill()
            process.wait(timeout=3)
        for process in self.processes:
            self.assertTrue(self.watch_done[process.pid].wait(3),
                            "清理前必须等自己的 watcher 关闭日志文件")

    def launch(self, delay, *arguments):
        with self.scheduler.lock:
            self.task["args"] = subprocess.list2cmdline(
                [str(self.marker), str(delay)] + list(arguments))
        return self.scheduler.fire(self.task, manual=True)

    def wait_for(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            threading.Event().wait(0.01)
        self.assertTrue(predicate(), "隔离进程未在限定时间内到达预期状态")

    def markers(self):
        if not self.marker.exists():
            return []
        result = []
        for line in self.marker.read_text(encoding="utf-8").splitlines():
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                # 主进程可能正好读取到子进程尚未写完的一行。
                continue
        return result

    def records(self):
        result = []
        for filename in self.directory.glob("runs-*.jsonl"):
            for line in filename.read_text(encoding="utf-8").splitlines():
                if line:
                    result.append(json.loads(line))
        return result

    def wait_finished(self, process):
        self.assertTrue(self.watch_done[process.pid].wait(4), "watcher 未按时结束")
        self.assertIsNotNone(process.poll())

    def restored_process(self, timeout=0, elapsed=0):
        # 模拟重启时遇到的子进程：由本测试直接创建，不真正崩溃任何调度器。
        process = subprocess.Popen(
            [sys.executable, str(self.script), str(self.marker), "4"],
            cwd=str(self.directory), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=app.CREATE_NO_WINDOW,
        )
        self.wait_for(lambda: bool(self.markers()))
        started = datetime.now() - timedelta(seconds=elapsed)
        snapshot = dict(self.task, timeout=timeout)
        self.task["active_run"] = {
            "pid": process.pid, "created": app.process_identity(process),
            "started": app.iso(started), "planned": app.iso(started),
            "missed": False, "task": snapshot,
        }
        self.assertTrue(app.save_config(self.scheduler.cfg))
        return process

    def test_real_process_success_preserves_arguments_and_writes_logs(self):
        arguments = ("", "含 空格", '内嵌"引号', " leading trailing ", "C:\\path with space\\")
        self.assertTrue(self.launch(0.05, *arguments))
        process = self.processes[-1]
        self.wait_finished(process)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(self.markers(), [{"pid": process.pid, "args": list(arguments)}])
        self.assertEqual([row["status"] for row in self.records()], ["success"])
        self.assertEqual(self.task["last_result"], app.STATUS_CN["success"])
        self.assertNotIn(self.task["id"], self.scheduler.running)
        output_files = list(self.directory.glob("run_*.log"))
        self.assertEqual(len(output_files), 1)
        self.assertIn("isolated task completed", output_files[0].read_text(encoding="utf-8"))

    def test_real_skip_new_keeps_one_process(self):
        self.task["conflict"] = "skip_new"
        self.assertTrue(self.launch(4))
        previous = self.processes[-1]
        self.wait_for(lambda: len(self.markers()) == 1)
        self.assertFalse(self.launch(0))
        self.assertEqual(len(self.processes), 1)
        self.assertIs(self.scheduler.running[self.task["id"]], previous)
        self.assertIsNone(previous.poll())
        self.assertEqual([row["status"] for row in self.records()], ["skipped"])
        self.assertTrue(self.scheduler.cancel_task(self.task["id"]))
        self.wait_finished(previous)

    def test_real_kill_previous_ends_old_process_and_completes_replacement(self):
        self.task["conflict"] = "kill_previous"
        self.assertTrue(self.launch(4))
        previous = self.processes[-1]
        self.wait_for(lambda: len(self.markers()) == 1)
        self.assertTrue(self.launch(0.05))
        replacement = self.processes[-1]
        self.assertNotEqual(previous.pid, replacement.pid)
        self.assertIsNotNone(previous.poll())
        self.wait_finished(previous)
        self.wait_finished(replacement)
        self.assertEqual(replacement.returncode, 0)
        self.assertEqual({row["pid"] for row in self.markers()}, {previous.pid, replacement.pid})
        self.assertEqual(sorted(row["status"] for row in self.records()), ["killed", "success"])
        self.assertEqual(self.task["last_result"], app.STATUS_CN["success"])
        self.assertNotIn(self.task["id"], self.scheduler.running)

    def test_real_timeout_terminates_process_and_records_timeout(self):
        self.task["timeout"] = 1
        self.assertTrue(self.launch(4))
        process = self.processes[-1]
        self.wait_finished(process)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual([row["status"] for row in self.records()], ["timeout"])
        self.assertEqual(self.task["last_result"], app.STATUS_CN["timeout"])
        self.assertNotIn(self.task["id"], self.scheduler.running)

    def test_real_delete_confirms_process_exit_before_removing_task(self):
        self.assertTrue(self.launch(4))
        process = self.processes[-1]
        self.wait_for(lambda: len(self.markers()) == 1)
        window = SimpleNamespace(
            sched=self.scheduler, cfg=self.scheduler.cfg,
            _selected_task=Mock(return_value=self.task), _is_running=Mock(return_value=True),
            _need_rebuild=False,
        )
        with patch.object(app.messagebox, "askyesno", return_value=True), \
                patch.object(app.messagebox, "showerror") as error, \
                patch.object(app.messagebox, "showwarning"):
            app.App._delete_task(window)
        error.assert_not_called()
        self.wait_finished(process)
        self.assertNotIn(self.task, self.scheduler.cfg["tasks"])
        self.assertNotIn(self.task["id"], self.scheduler.running)
        saved = json.loads(Path(app.CONFIG_PATH).read_text(encoding="utf-8"))
        self.assertEqual(saved["tasks"], [])

    def test_restore_real_process_blocks_duplicate_and_allows_confirmed_cancel(self):
        self.task["conflict"] = "skip_new"
        process = self.restored_process()
        self.scheduler._restore_running()
        attached = self.scheduler.running[self.task["id"]]
        self.assertEqual(attached.pid, process.pid)
        self.assertIsNone(attached.poll())
        self.assertFalse(self.launch(0))
        self.assertEqual(len(self.processes), 1)
        self.assertTrue(self.scheduler.cancel_task(self.task["id"]))
        self.wait_finished(process)
        self.assertNotIn(self.task["id"], self.scheduler.running)
        self.assertNotIn("active_run", self.task)
        # 已退出实例被 watcher 关闭句柄后仍应支持并发终止确认。
        self.assertIsNotNone(attached.poll())

    def test_restore_real_process_uses_remaining_original_timeout(self):
        self.task["timeout"] = 120  # 当前设置不能覆盖原始运行快照的超时。
        process = self.restored_process(timeout=2, elapsed=1)
        restore_at = time.monotonic()
        self.scheduler._restore_running()
        self.wait_finished(process)
        self.assertLess(time.monotonic() - restore_at, 2,
                        "接管后应只等待原超时剩余时间，而非重新等待完整超时")
        self.assertEqual([row["status"] for row in self.records()], ["timeout"])
        self.assertNotIn("active_run", self.task)
        self.assertNotIn(self.task["id"], self.scheduler.running)

    def test_restore_real_process_past_timeout_ends_immediately(self):
        self.task["timeout"] = 120
        process = self.restored_process(timeout=1, elapsed=3)
        restore_at = time.monotonic()
        self.scheduler._restore_running()
        self.wait_finished(process)
        self.assertLess(time.monotonic() - restore_at, 1.5,
                        "原超时已过的接管实例必须立即结束")
        self.assertEqual([row["status"] for row in self.records()], ["timeout"])
        self.assertNotIn("active_run", self.task)


if __name__ == "__main__":
    unittest.main()
