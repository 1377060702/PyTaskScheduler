"""在临时目录验证打包 EXE；仅启动自身的临时任务，结束时清理自己的进程树。"""

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


def main():
    source = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="pytask-packaged-") as folder:
        root = Path(folder)
        executable = root / "PyTaskScheduler.exe"
        shutil.copy2(source, executable)
        marker = root / "executions.txt"
        script = root / "worker.py"
        script.write_text(
            "from pathlib import Path\n"
            "import sys\n"
            "with Path(sys.argv[1]).open('a', encoding='utf-8') as handle:\n"
            "    handle.write('executed\\n')\n", encoding="utf-8")
        start = (datetime.now() + timedelta(seconds=8)).replace(microsecond=0)
        cfg = {"settings": {"python_dir": sys.executable, "watchdog_enabled": False,
                            "missed_policy": "skip", "log_retention_days": 0},
               "tasks": [{"id": "aabb00112233", "name": "隔离打包验证", "enabled": True,
                          "script": str(script), "args": subprocess.list2cmdline([str(marker)]),
                          "trigger_type": "daily", "start_date": start.date().isoformat(),
                          "trigger_time": start.strftime("%H:%M:%S"), "every_n_days": 1,
                          "daily_repeat_every": 1, "daily_repeat_duration": 3,
                          "console": "nowindow", "timeout": 5, "conflict": "skip_new"}]}
        (root / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        proc = subprocess.Popen([str(executable)], cwd=root, startupinfo=startup,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic() + 30
            records = []
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise AssertionError("打包程序提前退出：%s" % proc.returncode)
                records = []
                for log in (root / "logs").glob("runs-*.jsonl"):
                    for line in log.read_text(encoding="utf-8").splitlines():
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
                if len(records) >= 4 and datetime.now() > start + timedelta(seconds=5):
                    break
                time.sleep(0.1)
            assert len(records) == 4, records
            assert all(record["status"] == "success" for record in records), records
            expected = [(start + timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S") for i in range(4)]
            assert [record["planned"] for record in records] == expected, records
            assert marker.read_text(encoding="utf-8").splitlines() == ["executed"] * 4
            saved = json.loads((root / "config.json").read_text(encoding="utf-8"))
            assert saved["tasks"][0]["next_run"] == (start + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
            errors = root / "logs" / "errors.log"
            assert not errors.exists() or not errors.read_text(encoding="utf-8").strip()
            print(json.dumps({"executable": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                              "successful_runs": len(records), "planned": expected,
                              "next_run": saved["tasks"][0]["next_run"]}, ensure_ascii=False, indent=2))
        finally:
            if proc.poll() is None:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], check=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            proc.wait(timeout=10)
            # onefile 的子进程映像在 taskkill 返回后可能尚未解除文件映射。
            cleanup_deadline = time.monotonic() + 5
            while True:
                try:
                    executable.unlink(missing_ok=True)
                    break
                except PermissionError:
                    if time.monotonic() >= cleanup_deadline:
                        raise
                    time.sleep(0.1)


if __name__ == "__main__":
    main()
