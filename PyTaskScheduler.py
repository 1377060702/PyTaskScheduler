#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyTaskScheduler —— Windows 下 Python 脚本定时任务调度器

纯 Python 标准库实现（tkinter GUI + 后台调度线程），Win10 / Win11 均可运行。

功能：
  1. 全局配置 Python 目录（必填，不配置任务无法运行）
  2. 全局配置脚本基础目录（相对路径基准；未配置默认用户目录）
  3. 任务可选"无窗口纯后台"或"弹出 CMD 窗口"运行
  4. 每次运行写入 runs.jsonl（GUI 可查询），并按天写文本日志 logs/daily/YYYY-MM-DD.log
  5. 调度精确到秒（调度线程 200ms 一轮询）
  6. 任务列表 + 任务分组显示
  7. 触发类型：单次 / 每天 / 每隔 N 天
  8. 触发类型：每隔一段时间重复（可设持续时长，0=无限期）
  9. 超时强制结束（taskkill /T /F 杀进程树）
 10. 冲突策略：上一次未结束跳过本次 / 终止上一次立即运行新一次
 11. 任务导入导出（Windows 任务计划程序 XML 格式）
"""

import json
import heapq
import ctypes
import msvcrt
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import winreg
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, time as dtime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from windows_process import attach_process, process_identity

APP_NAME = "PyTask 调度器"

# 打包成 exe（PyInstaller）时，__file__ 指向临时解压目录，
# 必须用 sys.executable 所在目录定位程序根目录
IS_FROZEN = bool(getattr(sys, "frozen", False))
if IS_FROZEN:
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOGS_DIR = os.path.join(APP_DIR, "logs")
RUNS_LOG_PATH = os.path.join(LOGS_DIR, "runs.jsonl")
DAILY_LOG_DIR = os.path.join(LOGS_DIR, "daily")
ERROR_LOG_PATH = os.path.join(LOGS_DIR, "errors.log")

TIME_FMT = "%Y-%m-%d %H:%M:%S"
TS_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"

# 单实例锁 / 开机自启 / 看门狗
LOCK_PATH = os.path.join(APP_DIR, "scheduler.lock")
MAIN_SCRIPT = os.path.join(APP_DIR, "PyTaskScheduler.py")
WATCHDOG_SCRIPT = os.path.join(APP_DIR, "watchdog.py")
MAIN_EXE = os.path.join(APP_DIR, "PyTaskScheduler.exe")
WATCHDOG_EXE = os.path.join(APP_DIR, "watchdog.exe")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "PyTaskScheduler"
WATCHDOG_TASK_NAME = "PyTaskSchedulerWatchdog"
WATCHDOG_INTERVAL_MIN = 2   # 看门狗检测间隔（分钟）

# Windows 进程创建标志
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010

# 单次运行输出日志文件名：新 run_任务ID_YYYYMMDD.log / 旧 run_YYYYMMDD_HHMMSS_任务ID.log（清理用）
RE_RUN_LOG_NEW = re.compile(r"^run_[0-9a-f]{12}_(\d{8})\.log$")
RE_RUN_LOG_OLD = re.compile(r"^run_(\d{8})_\d{6}_[0-9a-f]{12}\.log$")

STATUS_CN = {
    "success": "成功",
    "failed": "失败",
    "timeout": "超时结束",
    "killed": "被终止",
    "skipped": "跳过",
    "error": "错误",
}

TASK_DEFAULTS = {
    "id": "",
    "name": "新任务",
    "group": "默认分组",
    "enabled": True,
    "finished": False,
    "script": "",
    "args": "",
    "workdir": "",
    "console": "nowindow",          # nowindow=纯后台 / window=弹 CMD 窗口
    "trigger_type": "daily",        # once / daily / interval
    "once_datetime": None,          # 单次：YYYY-MM-DD HH:MM:SS
    "trigger_time": "09:00:00",     # 每天：HH:MM:SS
    "every_n_days": 1,              # 每天：每隔 N 天
    "start_date": None,             # 每天：起始日期 YYYY-MM-DD（空=今天）
    "daily_repeat_every": 0,        # 每天：窗口内每 N 秒重复（0=每天仅一次）
    "daily_repeat_duration": 0,     # 每天：每轮重复窗口持续秒数
    "interval_start": None,         # 间隔：起始时间 YYYY-MM-DD HH:MM:SS
    "interval_every": 300,          # 间隔：每 N 秒
    "interval_duration": 0,         # 间隔：持续 N 秒（0=无限期）
    "timeout": 0,                   # 超时秒数（0=不限制）
    "conflict": "skip_new",         # skip_new / kill_previous
    "next_run": None,
    "last_run": None,
    "last_result": None,
    "last_run_duration": None,
}


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------

def now_str():
    return datetime.now().strftime(TIME_FMT)


def safe_int(v, default):
    """容错整数转换：config 手改损坏（非数字）时返回默认值，避免调度线程异常风暴"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _quote_arg(s):
    """Windows 参数引号化：含空格/引号的参数整体加引号（内部引号转义），用于 XML 导出与参数回存"""
    return subprocess.list2cmdline([str(s)])


def center_over_parent(win, parent):
    """把弹窗 win 居中到父窗口 parent 中央（限制在屏幕范围内）"""
    try:
        win.update_idletasks()   # 先让布局生效，拿到弹窗实际尺寸
        w, h = win.winfo_width(), win.winfo_height()
        px = parent.winfo_x() + (parent.winfo_width() - w) // 2
        py = parent.winfo_y() + (parent.winfo_height() - h) // 2
        px = max(0, min(px, win.winfo_screenwidth() - w))
        py = max(0, min(py, win.winfo_screenheight() - h))
        win.geometry("+%d+%d" % (px, py))
    except Exception:
        pass


def iso(dt):
    return dt.strftime(TIME_FMT) if dt else None


def parse_dt(s):
    if not s:
        return None
    s = str(s).strip()
    for fmt in (TIME_FMT, "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_date(s):
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_hms(s, default=(9, 0, 0)):
    """解析 HH:MM:SS（兼容 H:M / H），非法返回 default"""
    try:
        parts = [int(x) for x in str(s).strip().split(":") if str(x).strip() != ""]
        if not parts or len(parts) > 3:
            return default
        while len(parts) < 3:
            parts.append(0)
        h, m, sec = parts[0], parts[1], parts[2]
        if 0 <= h <= 23 and 0 <= m <= 59 and 0 <= sec <= 59:
            return h, m, sec
    except Exception:
        pass
    return default


def hms_valid(s):
    try:
        parts = [int(x) for x in str(s).strip().split(":") if str(x).strip() != ""]
        if not parts or len(parts) > 3:
            return False
        while len(parts) < 3:
            parts.append(0)
        h, m, sec = parts
        return 0 <= h <= 23 and 0 <= m <= 59 and 0 <= sec <= 59
    except Exception:
        return False


def human_secs(sec):
    sec = int(sec or 0)
    if sec <= 0:
        return "0 秒"
    if sec % 3600 == 0 and sec >= 3600:
        return "%d 小时" % (sec // 3600)
    if sec >= 3600:
        return "%d 小时 %d 分" % (sec // 3600, (sec % 3600) // 60)
    if sec % 60 == 0 and sec >= 60:
        return "%d 分钟" % (sec // 60)
    if sec >= 60:
        return "%d 分 %d 秒" % (sec // 60, sec % 60)
    return "%d 秒" % sec


def split_secs(sec):
    """秒数 -> (数值, 单位) 便于 UI 反显"""
    sec = int(sec or 0)
    if sec >= 3600 and sec % 3600 == 0:
        return sec // 3600, "小时"
    if sec >= 60 and sec % 60 == 0:
        return sec // 60, "分钟"
    return sec, "秒"


def parse_iso_duration(s):
    """ISO8601 时长 -> 秒（PnDTnHnMnS / PTnHnMnS），失败返回 0"""
    if not s:
        return 0
    m = re.match(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$", str(s).strip())
    if not m:
        return 0
    d = int(m.group(1) or 0)
    h = int(m.group(2) or 0)
    mi = int(m.group(3) or 0)
    se = float(m.group(4) or 0)
    return int(d * 86400 + h * 3600 + mi * 60 + se)


def fmt_iso_duration(sec):
    """秒数 -> ISO8601 时长字符串"""
    sec = int(sec or 0)
    if sec <= 0:
        return "PT0S"
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    out = "P"
    if d:
        out += "%dD" % d
    if h or m or s or not d:
        out += "T"
        if h:
            out += "%dH" % h
        if m:
            out += "%dM" % m
        if s:
            out += "%dS" % s
    return out


def parse_boundary(s):
    """任务计划程序 StartBoundary -> datetime（忽略毫秒/时区）"""
    if not s:
        return None
    base = str(s).strip().replace("Z", "").split("+")[0].split(".")[0]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(base, fmt)
        except ValueError:
            continue
    return None


def split_cmdline(s):
    """Windows 命令行参数拆分（保留带引号短语为整体）"""
    if not s or not str(s).strip():
        return []
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    split = shell32.CommandLineToArgvW
    split.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    split.restype = ctypes.POINTER(ctypes.c_wchar_p)
    free = kernel32.LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    count = ctypes.c_int()
    # argv[0] 的 Windows 解析规则不同，用占位程序名保证全部输入按参数处理。
    argv = split("pytask.exe " + str(s), ctypes.byref(count))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [argv[i] for i in range(1, count.value)]
    finally:
        free(ctypes.cast(argv, ctypes.c_void_p))


def kill_tree(pid):
    """强制结束进程树（含子进程）"""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:
        try:
            os.kill(pid, 9)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# 开机自启（HKCU Run 注册表）与看门狗（计划任务 + watchdog.py）
# ----------------------------------------------------------------------------

def resolve_launcher_python(cfg):
    """解析用于拉起主程序的解释器（pythonw 优先），返回 (路径, 错误)"""
    pd = str(cfg["settings"].get("python_dir") or "").strip()
    if not pd:
        return None, "未配置 Python 目录"
    base_dir = os.path.dirname(pd) if os.path.isfile(pd) else pd
    for name in ("pythonw.exe", "python.exe"):
        exe = os.path.join(base_dir, name)
        if os.path.isfile(exe):
            return exe, None
    return None, "在 %s 下未找到 pythonw.exe / python.exe" % base_dir


def autostart_state():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, AUTOSTART_NAME)
            return True
    except OSError:
        return False


def autostart_set(enable, cmd=None):
    """写 / 删开机自启注册表项（当前用户）。cmd 为完整启动命令字符串"""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, AUTOSTART_NAME)
            except FileNotFoundError:
                pass


def watchdog_state():
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", WATCHDOG_TASK_NAME],
                           creationflags=CREATE_NO_WINDOW, capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def watchdog_set(enable, run_cmd=None):
    """注册 / 删除看门狗计划任务（每 N 分钟运行 watchdog 检测并拉起调度器）"""
    if enable:
        cmd = ["schtasks", "/Create", "/F", "/TN", WATCHDOG_TASK_NAME,
               "/TR", run_cmd,
               "/SC", "MINUTE", "/MO", str(WATCHDOG_INTERVAL_MIN)]
    else:
        cmd = ["schtasks", "/Delete", "/F", "/TN", WATCHDOG_TASK_NAME]
    try:
        r = subprocess.run(cmd, creationflags=CREATE_NO_WINDOW,
                           capture_output=True, text=True, errors="replace", timeout=15)
        return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return False, str(e)


# ----------------------------------------------------------------------------
# 配置管理
# ----------------------------------------------------------------------------

CFG_LOCK = threading.RLock()


def normalize_task(t):
    for k, v in TASK_DEFAULTS.items():
        t.setdefault(k, v)
    if not t.get("id"):
        t["id"] = uuid.uuid4().hex[:12]
    _ensure_schedule_anchor(t, datetime.now())


def load_config():
    cfg = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("config 顶层必须是 JSON 对象")
        if not isinstance(cfg.get("tasks"), list):
            raise ValueError("tasks 必须是数组")
        cfg["tasks"] = [t for t in cfg["tasks"] if isinstance(t, dict)]
        if not isinstance(cfg.get("settings"), dict):
            raise ValueError("settings 必须是对象")
    except Exception:
        # 结构级损坏：备份原文件，从空配置启动（否则 noconsole 下崩溃循环被看门狗反复拉起）
        try:
            if os.path.isfile(CONFIG_PATH):
                os.replace(CONFIG_PATH, CONFIG_PATH + ".bad")
        except Exception:
            pass
        cfg = {"settings": {}, "tasks": []}
    settings = cfg.setdefault("settings", {})
    settings.setdefault("python_dir", "")
    settings.pop("scripts_base_dir", None)   # 已废弃：脚本路径直接使用完整路径
    settings.setdefault("missed_grace_seconds", 300)
    settings.setdefault("missed_policy", "run_once")     # run_once=错过补跑一次 / skip=跳过
    settings.setdefault("log_retention_days", 7)
    settings.setdefault("watchdog_enabled", True)        # 程序启动时自动注册看门狗
    tasks = cfg.setdefault("tasks", [])
    for t in tasks:
        normalize_task(t)
    return cfg


def save_config(cfg):
    with CFG_LOCK:
        try:
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, CONFIG_PATH)
            return True
        except Exception:
            # 磁盘满/权限问题时不能静默：至少落盘错误日志（_exhook 自带 5MB 轮转防刷爆）
            _exhook(*sys.exc_info())
            return False


# ----------------------------------------------------------------------------
# 触发时间计算
# ----------------------------------------------------------------------------

def _ensure_schedule_anchor(task, now_dt):
    """将空起点固定到配置中；旧任务优先沿用已保存计划的节律。"""
    tt = task.get("trigger_type", "daily")
    previous = parse_dt(task.get("next_run")) or parse_dt(task.get("last_run"))
    if tt == "daily" and parse_date(task.get("start_date")) is None:
        anchor = previous or now_dt
        start_day = anchor.date()
        if previous and safe_int(task.get("daily_repeat_every"), 0) > 0:
            # 跨午夜的重复格属于前一日开始的窗口。
            start = datetime.combine(start_day, dtime(*parse_hms(task.get("trigger_time"))))
            if anchor < start:
                start_day -= timedelta(days=1)
        task["start_date"] = start_day.isoformat()
    elif tt == "interval" and parse_dt(task.get("interval_start")) is None:
        task["interval_start"] = iso(previous or now_dt)


def _daily_window_start(task, reference):
    """返回 reference 所在周期的窗口起点，最早不早于已保存锚点。"""
    anchor = datetime.combine(parse_date(task["start_date"]),
                              dtime(*parse_hms(task.get("trigger_time"))))
    cycle = timedelta(days=max(1, safe_int(task.get("every_n_days"), 1)))
    count = max(0, (reference - anchor) // cycle)
    return anchor + count * cycle, cycle


def _grid_on_or_after(start, every, reference):
    if reference <= start:
        return start
    step = timedelta(seconds=every)
    count, remainder = divmod(reference - start, step)
    return start + (count + bool(remainder)) * step


def repetition_window_end(task, planned_dt):
    """返回某次计划所属的有限重复窗口末端；无有限窗口则返回 None。"""
    tt = task.get("trigger_type", "daily")
    if tt == "interval":
        duration = max(0, safe_int(task.get("interval_duration"), 0))
        start = parse_dt(task.get("interval_start"))
        return start + timedelta(seconds=duration) if start and duration else None
    if tt == "daily" and safe_int(task.get("daily_repeat_every"), 0) > 0:
        _ensure_schedule_anchor(task, planned_dt)
        start, _ = _daily_window_start(task, planned_dt)
        return start + timedelta(seconds=max(0, safe_int(task.get("daily_repeat_duration"), 0)))
    return None


def repetition_window_expired(task, planned_dt, now_dt):
    """终点整格保留一秒轮询容忍；其他旧格不能越过窗口末端补跑。"""
    end = repetition_window_end(task, planned_dt)
    if end is None or now_dt <= end:
        return False
    return planned_dt != end or now_dt > end + timedelta(seconds=1)


def compute_next_run(task, now_dt):
    """返回不早于 now_dt 的计划格；窗口末端若落在整格上则包含。"""
    tt = task.get("trigger_type", "daily")
    if tt == "once":
        planned = parse_dt(task.get("once_datetime"))
        return planned if planned is not None and planned >= now_dt else None
    _ensure_schedule_anchor(task, now_dt)
    if tt == "interval":
        start = parse_dt(task.get("interval_start"))
        every = max(1, safe_int(task.get("interval_every"), 60))
        dur = max(0, safe_int(task.get("interval_duration"), 0))
        nxt = _grid_on_or_after(start, every, now_dt)
        if dur > 0 and nxt > start + timedelta(seconds=dur):
            return None
        return nxt
    # daily：每天 / 每隔 N 天
    if tt != "daily":
        return None
    start, cycle = _daily_window_start(task, now_dt)
    try:
        every, duration = _daily_repeat_values(task.get("daily_repeat_every", 0),
                                               task.get("daily_repeat_duration", 0), cycle.days)
    except ValueError:
        return None
    if every == 0:
        return start if start >= now_dt else start + cycle
    nxt = _grid_on_or_after(start, every, now_dt)
    if nxt <= start + timedelta(seconds=duration):
        return nxt
    return start + cycle


def trigger_desc(task):
    tt = task.get("trigger_type", "daily")
    if tt == "once":
        return "单次 " + (task.get("once_datetime") or "未设置")
    if tt == "interval":
        every = human_secs(safe_int(task.get("interval_every"), 0) or 0)
        dur = max(0, safe_int(task.get("interval_duration"), 0))
        ds = human_secs(dur) if dur > 0 else "无限期"
        return "每 %s（持续 %s）" % (every, ds)
    n = max(1, safe_int(task.get("every_n_days"), 1))
    rep = "每天" if n == 1 else "每 %d 天" % n
    desc = "%s %s" % (rep, task.get("trigger_time", ""))
    every = max(0, safe_int(task.get("daily_repeat_every"), 0))
    if every:
        desc += "，每 %s重复（持续 %s）" % (
            human_secs(every), human_secs(safe_int(task.get("daily_repeat_duration"), 0)))
    return desc


# ----------------------------------------------------------------------------
# 调度器
# ----------------------------------------------------------------------------

class Scheduler(object):

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = CFG_LOCK
        self._launch_condition = threading.Condition(self.lock)
        self.log_lock = threading.Lock()
        self.running = {}          # task_id -> Popen
        self.stop_event = threading.Event()
        self._starting = set()     # 正在异步启动中的 task_id（防重复触发）
        self._cancelled = set()    # 已取消的待启动操作，删除失败时也不继续启动
        self._workers = set()
        self._closing = False
        self._generations = {}
        self._dirty = False        # 配置待落盘标记（高频任务下节流写盘）
        self.day_stats = {"day": datetime.now().strftime("%Y-%m-%d"),
                          "success": 0, "failed": 0, "skipped": 0, "error": 0, "timeout": 0}
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._recompute_all()

    # ---- 启动 / 停止 ------------------------------------------------------

    def start(self):
        self._restore_running()
        # 清理放后台线程：logs 目录文件多时避免阻塞 UI 启动
        threading.Thread(target=self._cleanup_runs_log, daemon=True).start()
        self.thread.start()
        self.flush_thread.start()

    def _restore_running(self):
        """接管有持久化身份且仍存活的实例，避免重启后绕过冲突和超时限制。"""
        with self.lock:
            for task in self.cfg["tasks"]:
                record = task.get("active_run")
                if not record or task["id"] in self.running:
                    continue
                try:
                    proc = attach_process(record["pid"], record["created"])
                    if proc is None:
                        task.pop("active_run", None)
                        task["last_result"] = "运行已结束（调度器离线期间，结果未知）"
                        continue
                    started = parse_dt(record.get("started"))
                    if started is None:
                        proc.close()
                        raise ValueError("恢复记录缺少有效开始时间")
                    snapshot = dict(record.get("task") or task)
                    snapshot["id"] = task["id"]
                    proc._pytask_started = started
                    self.running[task["id"]] = proc
                    task["last_result"] = "运行中（已恢复管理）"
                    threading.Thread(target=self._watch,
                                     args=(snapshot, proc, None, parse_dt(record.get("planned")) or started,
                                           bool(record.get("missed"))), daemon=True).start()
                except Exception:
                    task["enabled"] = False
                    self._cancelled.add(task["id"])
                    task["last_result"] = "错误：无法核对旧进程，已停用任务"
                    _exhook(*sys.exc_info())
            self._dirty = not save_config(self.cfg)

    def stop(self):
        with self.lock:
            self.stop_event.set()
            self._launch_condition.notify_all()
            workers = list(self._workers)
        # 禁止新进程先于等待；不等待任务本身结束。
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=1.0)

    def set_closing(self, closing):
        with self._launch_condition:
            self._closing = closing
            self._launch_condition.notify_all()

    def mark_dirty(self):
        self._dirty = True

    def _flush_loop(self):
        """配置落盘节流：高频任务（如每秒一跑）下避免每次触发都全量写盘。
        顺带跨天重跑日志清理：常驻数周不重启时保留天数策略不会失效。"""
        last_day = datetime.now().strftime("%Y-%m-%d")
        while not self.stop_event.wait(1.0):
            with self.lock:
                if self._dirty and save_config(self.cfg):
                    self._dirty = False
            day = datetime.now().strftime("%Y-%m-%d")
            if day != last_day:
                last_day = day
                try:
                    self._cleanup_runs_log()
                except Exception:
                    _exhook(*sys.exc_info())

    def _recompute_all(self):
        now = datetime.now()
        with self.lock:
            for t in self.cfg["tasks"]:
                try:
                    normalize_task(t)
                    if not t.get("enabled") or t.get("finished"):
                        continue
                    saved = parse_dt(t.get("next_run"))
                    if saved is not None and compute_next_run(t, saved) == saved:
                        # 保留有效的过去计划，交给同一套错过策略判断是否补跑。
                        nxt = saved
                    elif t.get("trigger_type") == "once":
                        nxt = parse_dt(t.get("once_datetime"))
                    else:
                        nxt = compute_next_run(t, now)
                except Exception:
                    _exhook(*sys.exc_info())
                    nxt = None
                t["next_run"] = iso(nxt)
                if nxt is None:
                    t["finished"] = True
            # 起点和恢复后的计划先落盘，避免每次启动重新定义节律。
            self._dirty = not save_config(self.cfg)

    def _cleanup_runs_log(self):
        """按保留天数清理按天滚动的日志文件（0=永久保留）"""
        try:
            days = int(self.cfg["settings"].get("log_retention_days", 7))
        except Exception:
            days = 7
        if days <= 0:
            return
        cutoff_str = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        cutoff_epoch = time.time() - days * 86400
        cutoff_run = cutoff_str.replace("-", "")
        try:
            for fn in os.listdir(LOGS_DIR):
                full = os.path.join(LOGS_DIR, fn)
                try:
                    if fn.startswith("runs-") and fn.endswith(".jsonl"):
                        if fn[5:-6] < cutoff_str:
                            os.remove(full)
                    elif fn == "runs.jsonl":  # 旧版单文件，按修改时间超期删除
                        if os.path.getmtime(full) < cutoff_epoch:
                            os.remove(full)
                    elif fn.startswith("run_") and fn.endswith(".log"):
                        # 单次运行输出（新 / 旧两种命名均按文件名中的日期清理）
                        m = RE_RUN_LOG_NEW.match(fn) or RE_RUN_LOG_OLD.match(fn)
                        if m and m.group(1) < cutoff_run:
                            os.remove(full)
                except OSError:
                    pass
            if os.path.isdir(DAILY_LOG_DIR):
                for fn in os.listdir(DAILY_LOG_DIR):
                    if fn.endswith(".log") and fn[:-4] < cutoff_str:
                        try:
                            os.remove(os.path.join(DAILY_LOG_DIR, fn))
                        except OSError:
                            pass
        except Exception:
            pass

    # ---- 主循环 ------------------------------------------------------------

    def _loop(self):
        """自适应轮询：平常 200ms 一轮；临近触发点时收窄等待，把触发抖动压到 ~10ms 内"""
        while not self.stop_event.is_set():
            try:
                nearest = self._tick()
            except Exception:
                _exhook(*sys.exc_info())
                nearest = None
            wait = 0.2
            if nearest is not None:
                remaining = nearest - time.time()
                if remaining < wait:
                    wait = max(0.005, remaining)
            self.stop_event.wait(wait)

    def _tick(self):
        """单轮调度。返回最近一次未来触发时刻的 epoch（无则 None）。"""
        now = datetime.now()
        now_epoch = time.time()
        with self.lock:
            grace = max(0, safe_int(self.cfg["settings"].get("missed_grace_seconds"), 300))
            policy = self.cfg["settings"].get("missed_policy", "run_once")
            tasks = list(self.cfg["tasks"])
        nearest = None
        for task in tasks:
            if self.stop_event.is_set():
                return nearest
            if not task.get("enabled") or task.get("finished"):
                continue
            try:
                nearest = self._tick_one(task, now, now_epoch, grace, policy, nearest)
            except Exception:
                # 单个任务数据损坏不拖垮整轮调度（后续任务照常），错误落盘
                _exhook(*sys.exc_info())
        return nearest

    def _tick_one(self, task, now, now_epoch, grace, policy, nearest):
        with self.lock:
            # UI 可能已替换、删除或停用本轮快照中的任务。
            if (self.stop_event.is_set() or self._closing
                    or not any(current is task for current in self.cfg["tasks"])
                    or not task.get("enabled") or task.get("finished")):
                return nearest
            nd = parse_dt(task.get("next_run"))
            if nd is None:
                nxt = (parse_dt(task.get("once_datetime")) if task.get("trigger_type") == "once"
                       else compute_next_run(task, now))
                task["next_run"] = iso(nxt)
                if nxt is None:
                    task["finished"] = True
                self._dirty = True
                if task.get("trigger_type") != "once" or nxt is None:
                    return nearest
                nd = nxt  # 新建/导入的过去单次计划也走同一套宽限判断。
            nd_epoch = nd.timestamp()
            if now < nd:
                return nd_epoch if nearest is None else min(nearest, nd_epoch)
            late = (now - nd).total_seconds()
            old_next, old_finished = task.get("next_run"), task.get("finished", False)
            if task.get("trigger_type") == "once":
                should_fire = late <= grace
                nxt = None
            else:
                should_fire = not repetition_window_expired(task, nd, now)
                should_fire = should_fire and not (late > grace and policy == "skip")
                # 消费本次格后直接进入未来；不逐格追赶，也不在整秒重复触发。
                nxt = compute_next_run(task, now + timedelta(microseconds=1))
            task["next_run"] = iso(nxt)
            task["finished"] = nxt is None
            # 在启动之前持久化消费结果；崩溃恢复不重放已经消费的同一格。
            if not save_config(self.cfg):
                task["next_run"], task["finished"] = old_next, old_finished
                self._dirty = True
                return nearest
            if should_fire:
                self._fire_async(task, nd, missed=late > grace)
            if nxt is not None:
                nearest = nxt.timestamp() if nearest is None else min(nearest, nxt.timestamp())
        return nearest

    # ---- 路径解析 ----------------------------------------------------------

    def resolve_python(self):
        pd = str(self.cfg["settings"].get("python_dir") or "").strip()
        if not pd:
            return None, "未配置 Python 目录（右上角 设置）"
        p = pd
        if os.path.isdir(p):
            p = os.path.join(p, "python.exe")
        if not os.path.isfile(p):
            return None, "Python 不存在：%s" % p
        return p, None

    def script_full_path(self, task):
        s = str(task.get("script") or "").strip()
        if not s:
            return None, "未配置脚本"
        if not os.path.isabs(s):
            # 相对路径按任务工作目录解析（与子进程实际的 cwd 一致，避免误报"脚本不存在"）
            wd = str(task.get("workdir") or "").strip()
            if wd:
                s = os.path.normpath(os.path.join(wd, s))
        return s, None

    # ---- 触发执行 ----------------------------------------------------------

    def _fire_async(self, task, planned_dt=None, missed=False, manual=False):
        """异步触发：调度线程只做占位判断，实际启动（Popen 等）放到工作线程，
        避免多个高频任务在同一轮触发时互相排队造成触发时刻漂移。"""
        tid = task["id"]
        with self.lock:  # 检查 + 占位必须原子：调度线程与 UI 手动"立即运行"可能同时进入
            if not self._can_launch(task, manual):
                return False
            if tid in self._starting:
                ts = datetime.now()
                self.log_run(task, planned_dt or ts, ts, ts, "skipped",
                             "上一次尚未完成启动，跳过本次", None, missed, 0.0)
                task["last_result"] = "跳过（上次未结束）"
                self._dirty = True
                return False
            self._starting.add(tid)
            generation = self._generations.get(tid, 0)
            worker = threading.Thread(
                target=self._fire_body, args=(task, planned_dt, missed, manual, generation),
                daemon=True,
            )
            self._workers.add(worker)
            try:
                worker.start()
            except Exception:
                self._workers.discard(worker)
                self._starting.discard(tid)
                raise
        return True

    def _can_launch(self, task, manual=False):
        """调用方持有 self.lock；手动运行允许停用任务，但不允许已删除任务。"""
        if self.stop_event.is_set() or self._closing or task["id"] in self._cancelled:
            return False
        current = next((t for t in self.cfg["tasks"] if t.get("id") == task["id"]), None)
        if current is not None and current.get("active_run") and task["id"] not in self.running:
            return False  # 未核实的旧进程必须先恢复，不能仅重新启用就绕过身份检查。
        return current is not None and (manual or bool(current.get("enabled")))

    def _fire_body(self, task, planned_dt, missed, manual, generation=None):
        try:
            self.fire(task, planned_dt, missed, manual, generation=generation)
        except Exception:
            _exhook(*sys.exc_info())
        finally:
            with self.lock:
                self._starting.discard(task["id"])
                self._workers.discard(threading.current_thread())

    def _kill_and_confirm(self, proc, wait_secs=5):
        """杀进程树并确认死亡。返回 True=确认已退出，False=未能杀死（权限拒绝等）"""
        kill_tree(proc.pid)
        try:
            proc.wait(wait_secs)
        except Exception:
            pass
        return proc.poll() is not None

    def fire(self, task, planned_dt=None, missed=False, manual=False, generation=None):
        """执行一次任务运行（应通过 _fire_async 调用，运行在工作线程中）。
        返回 True 表示已启动进程。"""
        if planned_dt is None:
            planned_dt = datetime.now()
        with self.lock:
            while self._closing and not self.stop_event.is_set():
                self._launch_condition.wait(0.2)
            if not self._can_launch(task, manual):
                return False
            if generation is None:
                generation = self._generations.get(task["id"], 0)
            if generation != self._generations.get(task["id"], 0):
                return False
            config_task = task
            task = dict(task)  # 运行参数快照，编辑不改变正在运行实例的超时和日志归属。
        exe, err = self.resolve_python()
        script = None
        if err is None:
            script, serr = self.script_full_path(task)
            if serr:
                err = serr
        if err:
            ts = datetime.now()
            self.log_run(task, planned_dt, ts, ts, "error", err, None, missed, 0.0)
            with self.lock:
                config_task["last_result"] = "错误：" + err
            self._dirty = True
            return False
        if not os.path.isfile(script):
            msg = "脚本不存在：%s" % script
            ts = datetime.now()
            self.log_run(task, planned_dt, ts, ts, "error", msg, None, missed, 0.0)
            with self.lock:
                config_task["last_result"] = "错误：" + msg
            self._dirty = True
            return False

        # 冲突策略：上一次还在运行
        with self.lock:
            while self._closing and not self.stop_event.is_set():
                self._launch_condition.wait(0.2)
            if not self._can_launch(task, manual) or generation != self._generations.get(task["id"], 0):
                return False
            proc = self.running.get(task["id"])
        if proc is not None and proc.poll() is None:
            if task.get("conflict") == "kill_previous":
                # 在进程对象上打标记：pid 可能被系统复用，对象引用不会认错
                with self.lock:
                    while self._closing and not self.stop_event.is_set():
                        self._launch_condition.wait(0.2)
                    if not self._can_launch(task, manual) or generation != self._generations.get(task["id"], 0):
                        return False
                    proc._pytask_killed = True
                    killed = self._kill_and_confirm(proc, 5)
                if not killed:
                    # 上一次没杀掉：绝不同时开第二个实例（违背冲突策略语义）
                    ts = datetime.now()
                    self.log_run(task, planned_dt, ts, ts, "error",
                                 "终止上一次运行失败，本次取消", None, missed, 0.0)
                    with self.lock:
                        config_task["last_result"] = "错误：终止上一次失败"
                    self._dirty = True
                    return False
                # 旧 watcher 会记录“被终止”，这里继续启动新一次
            else:
                ts = datetime.now()
                note = "上一次仍在运行，跳过本次" + ("（手动运行）" if manual else "")
                self.log_run(task, planned_dt, ts, ts, "skipped", note, None, missed, 0.0)
                with self.lock:
                    config_task["last_result"] = "跳过（上次未结束）"
                self._dirty = True
                return False

        # 工作目录校验
        cwd = str(task.get("workdir") or "").strip() or None
        if cwd and not os.path.isdir(cwd):
            msg = "工作目录不存在：%s" % cwd
            ts = datetime.now()
            self.log_run(task, planned_dt, ts, ts, "error", msg, None, missed, 0.0)
            with self.lock:
                config_task["last_result"] = "错误：" + msg
            self._dirty = True
            return False

        arg_list = []
        if str(task.get("args") or "").strip():
            # 使用 Windows 参数规则，保留空参数、内嵌引号和路径末尾反斜杠。
            arg_list = split_cmdline(str(task["args"])) or [str(task["args"])]
        cmd = [exe, script] + list(arg_list)

        lf = None
        try:
            # 复查和创建进程在同一把锁内，删除/退出不会穿过最后一次检查。
            with self.lock:
                while self._closing and not self.stop_event.is_set():
                    self._launch_condition.wait(0.2)
                if not self._can_launch(task, manual):
                    return False
                if generation != self._generations.get(task["id"], 0):
                    return False
                if not manual and repetition_window_expired(task, planned_dt, datetime.now()):
                    ts = datetime.now()
                    self.log_run(task, planned_dt, ts, ts, "skipped", "重复窗口已结束", None, missed, 0.0)
                    config_task["last_result"] = "跳过（重复窗口已结束）"
                    self._dirty = True
                    return False
                started = datetime.now()
                if task.get("console") == "window":
                    proc = subprocess.Popen(cmd, cwd=cwd, creationflags=CREATE_NEW_CONSOLE)
                else:
                    os.makedirs(LOGS_DIR, exist_ok=True)
                    day = started.strftime("%Y%m%d")
                    logfile = os.path.join(LOGS_DIR, "run_%s_%s.log" % (task["id"], day))
                    lf = open(logfile, "a", encoding="utf-8", errors="replace")
                    lf.write("\n===== [%s] 开始运行 =====\n" % started.strftime(TIME_FMT))
                    lf.flush()
                    proc = subprocess.Popen(
                        cmd, cwd=cwd, stdout=lf, stderr=lf,
                        stdin=subprocess.DEVNULL,
                        creationflags=CREATE_NO_WINDOW,
                    )
                proc._pytask_started = started
                self.running[task["id"]] = proc
                config_task["last_run"] = iso(started)
                config_task["last_result"] = "运行中…"
                config_task["last_run_duration"] = None
                self._dirty = True
                try:
                    identity = process_identity(proc)
                    config_task["active_run"] = {
                        "pid": proc.pid, "created": identity, "started": iso(started),
                        "planned": iso(planned_dt), "missed": bool(missed),
                        "task": {key: value for key, value in task.items() if key != "active_run"},
                    }
                    if not save_config(self.cfg):
                        raise OSError("运行进程身份保存失败")
                except Exception:
                    # 无法记录身份时不得继续放任一个重启后无法识别的实例运行。
                    config_task["enabled"] = False
                    self._cancelled.add(task["id"])
                    proc._pytask_launch_error = "运行身份保存失败，任务已停用"
                    proc._pytask_killed = True
                    self._kill_and_confirm(proc)
                    config_task["last_result"] = "错误：运行身份保存失败，任务已停用"
                    self._dirty = True
                    _exhook(*sys.exc_info())
        except Exception as e:
            if lf:
                try:
                    lf.close()
                except Exception:
                    pass
            ts = datetime.now()
            self.log_run(task, planned_dt, ts, ts, "error", "启动失败：%s" % e, None, missed, 0.0)
            with self.lock:
                config_task["last_result"] = "错误：启动失败"
            self._dirty = True
            return False

        threading.Thread(
            target=self._watch, args=(task, proc, lf, planned_dt, missed),
            daemon=True,
        ).start()
        return True

    def _watch(self, task, proc, lf, planned_dt, missed):
        start_dt = getattr(proc, "_pytask_started", datetime.now())
        status = None
        exit_code = None
        note = ""
        try:
            timeout = int(task.get("timeout") or 0)
        except Exception:
            timeout = 0
        try:
            if timeout > 0:
                remaining = max(0.0, timeout - (datetime.now() - start_dt).total_seconds())
                exit_code = proc.wait(timeout=remaining)
            else:
                exit_code = proc.wait()
        except subprocess.TimeoutExpired:
            if self._kill_and_confirm(proc, 5):
                status = "timeout"
                note = "运行超过 %d 秒，已强制结束" % timeout
            else:
                status = "error"
                note = "超时后强制结束失败，进程可能仍在运行"
        except Exception as e:
            status = "error"
            note = "等待进程失败：%s" % e
        end_dt = datetime.now()
        dur = (end_dt - start_dt).total_seconds()
        if getattr(proc, "_pytask_launch_error", None):
            status = "error"
            note = proc._pytask_launch_error
        elif status is None:
            if getattr(proc, "_pytask_killed", False):
                status = "killed"
                note = "被同任务的新一次运行终止"
            elif exit_code == 0:
                status = "success"
            else:
                status = "failed"
                note = "退出码 %s" % exit_code
        if lf:
            try:
                lf.close()
            except Exception:
                pass
        self.log_run(task, planned_dt, start_dt, end_dt, status, note, exit_code, missed, dur)
        with self.lock:
            if self.running.get(task["id"]) is proc:
                current = next((t for t in self.cfg["tasks"] if t.get("id") == task["id"]), None)
                if current is not None:
                    current["last_result"] = STATUS_CN.get(status, status)
                    current["last_run"] = iso(start_dt)
                    current["last_run_duration"] = round(dur, 1)
                # 强制终止失败时保留进程，继续用于下一次冲突判断。
                if proc.poll() is not None:
                    self.running.pop(task["id"], None)
                    if current is not None:
                        current.pop("active_run", None)
            self._dirty = True
        # 附着的进程句柄独立于 Popen，仅在其生命周期结束后关闭。
        if proc.poll() is not None and hasattr(proc, "close"):
            proc.close()

    # ---- 运行记录 ----------------------------------------------------------

    def log_run(self, task, planned_dt, start_dt, end_dt, status,
                note="", exit_code=None, missed=False, duration=0.0):
        tid = task.get("id")
        if tid and not any(x.get("id") == tid for x in self.cfg["tasks"]):
            return   # 任务已被删除（删除瞬间的在途触发），不写幽灵记录
        rec = {
            "time": now_str(),
            "task_id": task.get("id"),
            "task_name": task.get("name"),
            "group": task.get("group"),
            "script": task.get("script"),
            "planned": iso(planned_dt) if isinstance(planned_dt, datetime) else None,
            "start": iso(start_dt) if isinstance(start_dt, datetime) else None,
            "end": iso(end_dt) if isinstance(end_dt, datetime) else None,
            "duration": round(float(duration or 0), 3),
            "exit_code": exit_code,
            "status": status,
            "status_cn": STATUS_CN.get(status, status),
            "missed": bool(missed),
            "note": note,
        }
        day = start_dt.strftime("%Y-%m-%d") if isinstance(start_dt, datetime) else now_str()[:10]
        daily_line = "[%s] [%s] 任务「%s」 %s 耗时 %.1f 秒 退出码:%s%s%s" % (
            start_dt.strftime(TIME_FMT) if isinstance(start_dt, datetime) else now_str(),
            STATUS_CN.get(status, status),
            task.get("name"),
            task.get("script"),
            float(duration or 0),
            ("-" if exit_code is None else exit_code),
            "（补跑）" if missed else "",
            ("  " + note) if note else "",
        )
        runs_path = os.path.join(LOGS_DIR, "runs-%s.jsonl" % day)  # 按天滚动，防单文件无限膨胀
        with self.log_lock:
            today = datetime.now().strftime("%Y-%m-%d")
            if self.day_stats["day"] != today:
                self.day_stats = {"day": today, "success": 0, "failed": 0,
                                  "skipped": 0, "error": 0, "timeout": 0}
            self.day_stats[status] = self.day_stats.get(status, 0) + 1
            try:
                with open(runs_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:
                pass
            try:
                os.makedirs(DAILY_LOG_DIR, exist_ok=True)
                with open(os.path.join(DAILY_LOG_DIR, day + ".log"), "a", encoding="utf-8") as f:
                    f.write(daily_line + "\n")
            except Exception:
                pass

    def kill_running(self, task_id):
        with self.lock:
            proc = self.running.get(task_id)
            if proc is None and any(t.get("id") == task_id and t.get("active_run")
                                    for t in self.cfg["tasks"]):
                return False  # 身份尚未核实，不得当作已退出并删除唯一的恢复记录。
        if proc is not None and proc.poll() is None:
            proc._pytask_killed = True
            return self._kill_and_confirm(proc)
        return True

    def cancel_task(self, task_id):
        """先取消待启动，再确认现有实例结束；失败保留任务和进程的管理关系。"""
        with self.lock:
            self._cancelled.add(task_id)
            self._generations[task_id] = self._generations.get(task_id, 0) + 1
        if not self.kill_running(task_id):
            with self.lock:
                for task in self.cfg["tasks"]:
                    if task.get("id") == task_id:
                        task["enabled"] = False
                        task["last_result"] = "错误：终止失败，已停用后续调度"
                self._dirty = True
            return False
        return True


# ----------------------------------------------------------------------------
# Windows 任务计划程序 XML 导入 / 导出
# ----------------------------------------------------------------------------

def _q(tag):
    return "{%s}%s" % (TS_NS, tag)


def _schedule_integer(value, label, minimum=0):
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]+", text) or int(text) < minimum:
        raise ValueError("%s必须是%s整数。" % (label, "正" if minimum else "非负"))
    return int(text)


def _daily_repeat_values(every, duration, days):
    every = _schedule_integer(every, "重复间隔")
    if not every:
        return 0, 0
    duration = _schedule_integer(duration, "重复持续时间", 1)
    if every > duration:
        raise ValueError("重复间隔不能大于持续时间。")
    if duration > days * 86400:
        raise ValueError("重复持续时间不能超过每隔 N 天的周期。")
    return every, duration


def _xml_duration_seconds(value, label, minimum=0):
    value = str(value).strip()
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value)
    if not match or not any(match.groups()) or value.endswith("T"):
        raise ValueError("%s不支持或格式错误（只支持天、时、分和整数秒）：%s" % (label, value))
    seconds = parse_iso_duration(value)
    if seconds < minimum:
        raise ValueError("%s不得少于 %d 秒。" % (label, minimum))
    return seconds


def import_task_xml(path):
    """
    解析 Windows 任务计划程序导出的 XML。
    返回 (task_dict 或 None, python_dir 或 None, warning 或 None)
    """
    try:
        tree = ET.parse(path)
    except Exception as e:
        return None, None, "XML 解析失败：%s" % e
    root = tree.getroot()

    # ---- 动作：<Exec><Command><Arguments><WorkingDirectory>
    actions = root.find(_q("Actions"))
    if actions is None or len(actions) != 1 or actions[0].tag != _q("Exec"):
        return None, None, "仅支持恰好一个执行 Python 脚本的 Exec 动作。"
    cmd, args_s, workdir = None, "", ""
    for exec_el in root.iter(_q("Exec")):
        c = (exec_el.findtext(_q("Command"), default="") or "").strip().strip('"').strip()
        a = (exec_el.findtext(_q("Arguments"), default="") or "").strip()
        w = (exec_el.findtext(_q("WorkingDirectory"), default="") or "").strip()
        if c:
            cmd, args_s, workdir = c, a, w
            break
    if not cmd:
        return None, None, "XML 中没有可用的 <Exec> 动作"

    lower = os.path.basename(cmd).lower()
    args_tokens = split_cmdline(args_s)
    script = None
    extra_args = []
    python_dir_found = None
    if lower.endswith(".py") or lower.endswith(".pyw"):
        script = cmd
        extra_args = args_tokens
    elif lower.startswith("python"):
        python_dir_found = os.path.dirname(cmd) or None
        for i, tok in enumerate(args_tokens):
            tl = tok.lower()
            if tl.endswith(".py") or tl.endswith(".pyw"):
                script = tok
                extra_args = args_tokens[i + 1:]
                break
        if script is None and args_tokens:
            script = args_tokens[0]
            extra_args = args_tokens[1:]
    else:
        return None, None, "仅支持执行 Python 脚本的任务（Command=%s）" % cmd
    if not script:
        return None, None, "未能在任务中找到 Python 脚本"

    # ---- 触发器
    warnings = []
    triggers_el = root.find(_q("Triggers"))
    if triggers_el is None or len(triggers_el) != 1:
        return None, python_dir_found, "仅支持恰好一个触发器；多触发器任务请先拆分。"
    el = triggers_el[0]
    tag = el.tag.split("}")[-1]
    if el.tag not in (_q("TimeTrigger"), _q("CalendarTrigger")):
        return None, python_dir_found, "不支持的触发器：%s" % tag
    try:
        allowed = {"Enabled", "StartBoundary", "EndBoundary", "Repetition", "ExecutionTimeLimit", "RandomDelay"}
        if tag == "CalendarTrigger":
            allowed.add("ScheduleByDay")
        seen = set()
        for child in el:
            child_tag = child.tag.split("}")[-1]
            if child.tag != _q(child_tag) or child_tag not in allowed or child_tag in seen:
                raise ValueError("不支持或重复的触发器参数：%s" % child_tag)
            seen.add(child_tag)
        if el.find(_q("EndBoundary")) is not None:
            raise ValueError("暂不支持设有 EndBoundary 截止日期的触发器。")
        for field in ("RandomDelay", "ExecutionTimeLimit"):
            value = el.findtext(_q(field))
            if value is not None and _xml_duration_seconds(value, field) != 0:
                raise ValueError("暂不支持触发器参数 %s。" % field)
        sb = (el.findtext(_q("StartBoundary"), default="") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.0+)?)?", sb):
            raise ValueError("StartBoundary 必须是本地日期时间；不支持时区或非整数秒。")
        start_b = datetime.fromisoformat(sb)
        trigger_enabled = (el.findtext(_q("Enabled"), default="true") or "").strip().lower()
        if trigger_enabled not in ("true", "false", "1", "0"):
            raise ValueError("触发器 Enabled 值无效。")
        rep = el.find(_q("Repetition"))
        every, duration = 0, 0
        if rep is not None:
            rep_tags = [child.tag for child in rep]
            if (len(rep_tags) != len(set(rep_tags)) or
                    any(x not in {_q("Interval"), _q("Duration"), _q("StopAtDurationEnd")} for x in rep_tags)):
                raise ValueError("Repetition 中包含重复或不支持的参数。")
            every = _xml_duration_seconds(rep.findtext(_q("Interval"), default=""), "重复间隔", 60)
            if every > 31 * 86400:
                raise ValueError("Windows XML 重复间隔不能超过 31 天。")
            dur_s = rep.findtext(_q("Duration"))
            duration = _xml_duration_seconds(dur_s, "重复持续时间", 60) if dur_s is not None else 0
            stop = (rep.findtext(_q("StopAtDurationEnd"), default="false") or "").strip().lower()
            if stop not in ("false", "0"):
                raise ValueError("不支持 StopAtDurationEnd：本程序在窗口结束后不强制终止正在运行的任务。")
            if duration and every > duration:
                raise ValueError("重复间隔不能大于持续时间。")
        if tag == "CalendarTrigger":
            byday = el.find(_q("ScheduleByDay"))
            if byday is None or any(child.tag != _q("DaysInterval") for child in byday) or len(byday) > 1:
                raise ValueError("仅支持每天 / 每隔 N 天的 ScheduleByDay 触发器。")
            n = _schedule_integer(byday.findtext(_q("DaysInterval"), default="1"), "天数", 1)
            if n > 365:
                raise ValueError("Windows XML 每隔天数不能超过 365 天。")
            every, duration = _daily_repeat_values(every, duration, n)
            chosen = {"type": "daily", "time": start_b.time(), "n": n,
                      "start_date": start_b.date(), "every": every, "duration": duration}
        elif rep is not None:
            chosen = {"type": "interval", "start": start_b, "every": every, "duration": duration}
        else:
            chosen = {"type": "once", "dt": start_b}
    except (ValueError, OverflowError) as e:
        return None, python_dir_found, "无法导入触发器：%s" % e

    # ---- 设置
    timeout = 72 * 3600  # Windows Task Scheduler 的缺省 ExecutionTimeLimit。
    conflict = "skip_new"
    enabled = True
    settings_el = root.find(_q("Settings"))
    if settings_el is not None:
        try:
            etl = settings_el.findtext(_q("ExecutionTimeLimit"))
            if etl is not None:
                timeout = _xml_duration_seconds(etl, "运行超时限制")
            mip = (settings_el.findtext(_q("MultipleInstancesPolicy"), default="IgnoreNew") or "").strip()
            if mip not in ("IgnoreNew", "StopExisting"):
                raise ValueError("不支持实例策略 %s；仅支持 IgnoreNew / StopExisting。" % mip)
            if mip == "StopExisting":
                conflict = "kill_previous"
            en = (settings_el.findtext(_q("Enabled"), default="true") or "").strip().lower()
            if en not in ("true", "false", "1", "0"):
                raise ValueError("任务 Enabled 值无效。")
            enabled = en not in ("false", "0")
        except (ValueError, OverflowError) as e:
            return None, python_dir_found, "无法导入设置：%s" % e
    enabled = enabled and trigger_enabled not in ("false", "0")

    # ---- 任务名：URI 末段 > Description > 文件名
    uri = (root.findtext(_q("RegistrationInfo") + "/" + _q("URI"), default="") or "").strip()
    name = ""
    if uri:
        name = uri.replace("\\", "/").rstrip("/").split("/")[-1]
    if not name:
        name = (root.findtext(_q("RegistrationInfo") + "/" + _q("Description"), default="") or "").strip()
    if not name:
        name = os.path.splitext(os.path.basename(path))[0]
    name = name[:80]

    t = dict(TASK_DEFAULTS)
    t["id"] = uuid.uuid4().hex[:12]
    t["name"] = name
    t["group"] = "导入"
    t["enabled"] = enabled
    t["finished"] = False
    t["script"] = script
    t["args"] = " ".join(_quote_arg(x) for x in extra_args)
    t["workdir"] = workdir or ""
    t["console"] = "nowindow"
    t["timeout"] = timeout
    t["conflict"] = conflict
    if chosen["type"] == "once":
        t["trigger_type"] = "once"
        t["once_datetime"] = chosen["dt"].strftime(TIME_FMT)
    elif chosen["type"] == "daily":
        t["trigger_type"] = "daily"
        t["trigger_time"] = chosen["time"].strftime("%H:%M:%S")
        t["every_n_days"] = chosen["n"]
        t["start_date"] = chosen["start_date"].strftime("%Y-%m-%d")
        t["daily_repeat_every"] = chosen["every"]
        t["daily_repeat_duration"] = chosen["duration"]
    else:
        t["trigger_type"] = "interval"
        t["interval_start"] = chosen["start"].strftime(TIME_FMT)
        t["interval_every"] = chosen["every"]
        t["interval_duration"] = chosen["duration"]
    t["next_run"] = iso(compute_next_run(t, datetime.now()))
    return t, python_dir_found, ("\n".join(warnings) if warnings else None)


def _xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def export_task_xml(task, python_exe, path):
    """导出支持的触发器；无法满足 Windows XML 限制时明确拒绝。"""
    name = task.get("name") or task["id"]
    en = "true" if task.get("enabled", True) else "false"

    def repetition_lines(every, duration):
        if not 60 <= every <= 31 * 86400:
            raise ValueError("Windows XML 重复间隔必须在 1 分钟至 31 天之间。")
        if duration and (duration < 60 or every > duration):
            raise ValueError("Windows XML 持续时间至少为 1 分钟，且不能小于重复间隔。")
        lines = ["      <Repetition>",
                 "        <Interval>%s</Interval>" % fmt_iso_duration(every)]
        if duration:
            lines.append("        <Duration>%s</Duration>" % fmt_iso_duration(duration))
        return lines + ["        <StopAtDurationEnd>false</StopAtDurationEnd>", "      </Repetition>"]

    tt = task.get("trigger_type", "daily")
    if tt == "once":
        dt = parse_dt(task.get("once_datetime") or "")
        if dt is None:
            raise ValueError("单次运行日期时间无效。")
        trig_lines = [
            "    <TimeTrigger>",
            "      <Enabled>%s</Enabled>" % en,
            "      <StartBoundary>%s</StartBoundary>" % dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "    </TimeTrigger>",
        ]
    elif tt == "daily":
        if not re.fullmatch(r"\d{1,2}(?::\d{1,2}){0,2}", str(task.get("trigger_time") or "").strip()) or not hms_valid(task.get("trigger_time")):
            raise ValueError("每天的开始时间无效。")
        h, m, s = parse_hms(task.get("trigger_time"))
        n = _schedule_integer(task.get("every_n_days", 1), "每隔天数", 1)
        if n > 365:
            raise ValueError("Windows XML 每隔天数必须在 1 至 365 之间。")
        sd = parse_date(task.get("start_date")) if task.get("start_date") else datetime.now().date()
        if sd is None:
            raise ValueError("起始日期无效。")
        sd = sd.isoformat()
        every, duration = _daily_repeat_values(task.get("daily_repeat_every", 0),
                                             task.get("daily_repeat_duration", 0), n)
        sb = "%sT%02d:%02d:%02d" % (sd, h, m, s)
        trig_lines = [
            "    <CalendarTrigger>",
            "      <Enabled>%s</Enabled>" % en,
            "      <StartBoundary>%s</StartBoundary>" % sb,
        ]
        if every:
            trig_lines += repetition_lines(every, duration)
        trig_lines += [
            "      <ScheduleByDay>",
            "        <DaysInterval>%d</DaysInterval>" % n,
            "      </ScheduleByDay>",
            "    </CalendarTrigger>",
        ]
    elif tt == "interval":
        start = parse_dt(task.get("interval_start") or "")
        if start is None:
            raise ValueError("重复任务的起始日期时间无效。")
        every = _schedule_integer(task.get("interval_every", 300), "重复间隔", 1)
        duration = _schedule_integer(task.get("interval_duration", 0), "重复持续时间")
        trig_lines = [
            "    <TimeTrigger>",
            "      <Enabled>%s</Enabled>" % en,
            "      <StartBoundary>%s</StartBoundary>" % start.strftime("%Y-%m-%dT%H:%M:%S"),
        ] + repetition_lines(every, duration) + ["    </TimeTrigger>"]
    else:
        raise ValueError("不支持的触发类型：%s" % tt)

    mip = "StopExisting" if task.get("conflict") == "kill_previous" else "IgnoreNew"
    etl = fmt_iso_duration(_schedule_integer(task.get("timeout", 0), "超时限制"))

    args_tokens = [str(task.get("script") or "")]
    if str(task.get("args") or "").strip():
        args_tokens += split_cmdline(str(task["args"]))
    args_str = " ".join(_quote_arg(x) for x in args_tokens)
    workdir = str(task.get("workdir") or "").strip()

    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Task version="1.2" xmlns="%s">' % TS_NS,
        "  <RegistrationInfo>",
        "    <Description>%s</Description>" % _xml_escape(name),
        "    <URI>\\PyTask\\%s</URI>" % _xml_escape(name),
        "  </RegistrationInfo>",
        "  <Triggers>",
    ]
    xml_lines += trig_lines
    xml_lines += [
        "  </Triggers>",
        "  <Settings>",
        "    <MultipleInstancesPolicy>%s</MultipleInstancesPolicy>" % mip,
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
        "    <AllowHardTerminate>true</AllowHardTerminate>",
        "    <StartWhenAvailable>false</StartWhenAvailable>",
        "    <AllowStartOnDemand>true</AllowStartOnDemand>",
        "    <Enabled>%s</Enabled>" % en,
        "    <Hidden>false</Hidden>",
        "    <ExecutionTimeLimit>%s</ExecutionTimeLimit>" % etl,
        "    <Priority>7</Priority>",
        "  </Settings>",
        "  <Actions>",
        "    <Exec>",
        "      <Command>%s</Command>" % _xml_escape(python_exe),
        "      <Arguments>%s</Arguments>" % _xml_escape(args_str),
    ]
    if workdir:
        xml_lines.append("      <WorkingDirectory>%s</WorkingDirectory>" % _xml_escape(workdir))
    xml_lines += [
        "    </Exec>",
        "  </Actions>",
        "</Task>",
    ]
    xml = "\n".join(xml_lines) + "\n"
    # 自检：确保是合法 XML
    ET.fromstring(xml.encode("utf-8"))
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(xml)


# ----------------------------------------------------------------------------
# 全局异常钩子（pythonw 无控制台，异常写入文件）
# ----------------------------------------------------------------------------

def _exhook(exc_type, exc_val, exc_tb):
    try:
        import traceback
        # 超过 5MB 轮转，防止异常风暴（如 config 损坏导致每 200ms 一次）撑爆磁盘
        try:
            if os.path.isfile(ERROR_LOG_PATH) and os.path.getsize(ERROR_LOG_PATH) > 5 * 1024 * 1024:
                os.replace(ERROR_LOG_PATH, ERROR_LOG_PATH + ".old")
        except Exception:
            pass
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write("\n[%s]\n%s" % (now_str(), "".join(traceback.format_exception(exc_type, exc_val, exc_tb))))
    except Exception:
        pass


sys.excepthook = _exhook


def _thread_exhook(args):
    _exhook(args.exc_type, args.exc_value, args.exc_traceback)


threading.excepthook = _thread_exhook


# ----------------------------------------------------------------------------
# GUI：任务编辑对话框
# ----------------------------------------------------------------------------

TRIGGER_LABELS = ["单次（指定日期时间）", "每天 / 每隔 N 天", "每隔一段时间重复"]
TRIGGER_KEYS = ["once", "daily", "interval"]
CONFLICT_LABELS = {"skip_new": "上一次未结束则跳过本次", "kill_previous": "终止上一次并立即运行新一次"}
UNIT_SECONDS = {"秒": 1, "分钟": 60, "小时": 3600}
UNIT_ORDER = ["秒", "分钟", "小时"]


class TaskDialog(tk.Toplevel):

    def __init__(self, master, task, groups):
        super().__init__(master)
        self.task = task
        self.ok = False
        self.title("编辑任务" if task.get("id") in [t["id"] for t in master.cfg["tasks"]] else "新增任务")
        self.resizable(False, False)
        self.grab_set()
        self.transient(master)

        pad = {"padx": 8, "pady": 4}
        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)

        # ---- 基本
        row = 0
        ttk.Label(body, text="任务名称：").grid(row=row, column=0, sticky="e", **pad)
        self.var_name = tk.StringVar(value=task.get("name") or "")
        ttk.Entry(body, textvariable=self.var_name, width=32).grid(row=row, column=1, sticky="w", **pad)
        ttk.Label(body, text="分组：").grid(row=row, column=2, sticky="e", **pad)
        self.var_group = tk.StringVar(value=task.get("group") or "默认分组")
        ttk.Combobox(body, textvariable=self.var_group, values=groups, width=16).grid(row=row, column=3, sticky="w", **pad)
        row += 1

        # ---- 脚本
        ttk.Separator(body).grid(row=row, column=0, columnspan=4, sticky="ew", pady=6)
        row += 1
        ttk.Label(body, text="脚本路径：").grid(row=row, column=0, sticky="e", **pad)
        sf = ttk.Frame(body)
        sf.grid(row=row, column=1, columnspan=3, sticky="we", **pad)
        self.var_script = tk.StringVar(value=task.get("script") or "")
        ttk.Entry(sf, textvariable=self.var_script, width=52).pack(side="left", fill="x", expand=True)
        ttk.Button(sf, text="浏览…", width=7, command=self._browse_script).pack(side="left", padx=(4, 0))
        row += 1
        ttk.Label(body, text="（Python 脚本完整路径）", foreground="#777").grid(row=row, column=1, columnspan=3, sticky="w")
        row += 1
        ttk.Label(body, text="运行参数：").grid(row=row, column=0, sticky="e", **pad)
        self.var_args = tk.StringVar(value=task.get("args") or "")
        ttk.Entry(body, textvariable=self.var_args, width=54).grid(row=row, column=1, columnspan=3, sticky="we", **pad)
        row += 1
        ttk.Label(body, text="工作目录：").grid(row=row, column=0, sticky="e", **pad)
        wf = ttk.Frame(body)
        wf.grid(row=row, column=1, columnspan=3, sticky="we", **pad)
        self.var_workdir = tk.StringVar(value=task.get("workdir") or "")
        ttk.Entry(wf, textvariable=self.var_workdir, width=52).pack(side="left", fill="x", expand=True)
        ttk.Button(wf, text="浏览…", width=7, command=self._browse_workdir).pack(side="left", padx=(4, 0))
        row += 1
        ttk.Label(body, text="窗口方式：").grid(row=row, column=0, sticky="e", **pad)
        self.var_console = tk.StringVar(value=task.get("console") or "nowindow")
        cf = ttk.Frame(body)
        cf.grid(row=row, column=1, columnspan=3, sticky="w", **pad)
        ttk.Radiobutton(cf, text="无窗口（纯后台，无 CMD 弹窗）", variable=self.var_console, value="nowindow").pack(side="left")
        ttk.Radiobutton(cf, text="弹出 CMD 窗口", variable=self.var_console, value="window").pack(side="left", padx=(12, 0))
        row += 1

        # ---- 触发
        ttk.Separator(body).grid(row=row, column=0, columnspan=4, sticky="ew", pady=6)
        row += 1
        ttk.Label(body, text="触发类型：").grid(row=row, column=0, sticky="e", **pad)
        cur_key = task.get("trigger_type", "daily")
        cur_idx = TRIGGER_KEYS.index(cur_key) if cur_key in TRIGGER_KEYS else 1
        self.var_trigger = tk.StringVar(value=TRIGGER_LABELS[cur_idx])
        cb = ttk.Combobox(body, textvariable=self.var_trigger, values=TRIGGER_LABELS,
                          state="readonly", width=22)
        cb.grid(row=row, column=1, columnspan=3, sticky="w", **pad)
        cb.bind("<<ComboboxSelected>>", lambda e: self._switch_trigger())
        row += 1

        self.trig_holder = ttk.Frame(body)
        self.trig_holder.grid(row=row, column=0, columnspan=4, sticky="we", padx=16)
        self._build_trigger_frames()
        row += 1

        # ---- 运行控制
        ttk.Separator(body).grid(row=row, column=0, columnspan=4, sticky="ew", pady=6)
        row += 1
        ttk.Label(body, text="超时限制：").grid(row=row, column=0, sticky="e", **pad)
        tf = ttk.Frame(body)
        tf.grid(row=row, column=1, sticky="w", **pad)
        self.var_timeout = tk.StringVar(value=str(int(task.get("timeout") or 0)))
        ttk.Spinbox(tf, from_=0, to=86400 * 30, textvariable=self.var_timeout, width=9).pack(side="left")
        ttk.Label(tf, text=" 秒（0 = 不限制）").pack(side="left")
        row += 1
        ttk.Label(body, text="冲突策略：").grid(row=row, column=0, sticky="e", **pad)
        conflict = task.get("conflict") or "skip_new"
        self.var_conflict = tk.StringVar(value=CONFLICT_LABELS[conflict])
        ttk.Combobox(body, textvariable=self.var_conflict, values=list(CONFLICT_LABELS.values()),
                     state="readonly", width=30).grid(row=row, column=1, columnspan=3, sticky="w", **pad)
        row += 1

        # ---- 按钮
        bf = ttk.Frame(body)
        bf.grid(row=row, column=0, columnspan=4, pady=(12, 0))
        ttk.Button(bf, text="保存", command=self._on_save).pack(side="left", padx=8)
        ttk.Button(bf, text="取消", command=self.destroy).pack(side="left", padx=8)

        self._switch_trigger()
        self.bind("<Return>", lambda e: self._on_save())
        self.bind("<Escape>", lambda e: self.destroy())
        center_over_parent(self, master)

    # ------------------------------------------------------------------

    def _build_trigger_frames(self):
        t = self.task
        # 单次
        self.f_once = ttk.Frame(self.trig_holder)
        dt = parse_dt(t.get("once_datetime") or "")
        default_dt = datetime.now() + timedelta(days=1)
        self.once_date = tk.StringVar(value=(dt or default_dt).strftime("%Y-%m-%d"))
        self.once_time = tk.StringVar(value=(dt or default_dt).strftime("%H:%M:%S"))
        ttk.Label(self.f_once, text="日期：").pack(side="left")
        ttk.Entry(self.f_once, textvariable=self.once_date, width=13).pack(side="left", padx=(0, 10))
        ttk.Label(self.f_once, text="时间：").pack(side="left")
        ttk.Entry(self.f_once, textvariable=self.once_time, width=10).pack(side="left")
        ttk.Label(self.f_once, text="（YYYY-MM-DD  HH:MM:SS，精确到秒）", foreground="#777").pack(side="left", padx=(6, 0))

        # 每天 / 每隔 N 天
        self.f_daily = ttk.Frame(self.trig_holder)
        self.daily_time = tk.StringVar(value=t.get("trigger_time") or "09:00:00")
        self.daily_n = tk.StringVar(value=str(t.get("every_n_days", 1)))
        self.daily_start = tk.StringVar(value=t.get("start_date") or "")
        daily_row = ttk.Frame(self.f_daily)
        daily_row.pack(anchor="w")
        ttk.Label(daily_row, text="开始时间：").pack(side="left")
        ttk.Entry(daily_row, textvariable=self.daily_time, width=10).pack(side="left", padx=(0, 10))
        ttk.Label(daily_row, text="每隔").pack(side="left")
        ttk.Spinbox(daily_row, from_=1, to=9999, textvariable=self.daily_n, width=6).pack(side="left", padx=2)
        ttk.Label(daily_row, text="天触发").pack(side="left", padx=(0, 10))
        ttk.Label(daily_row, text="起始日期：").pack(side="left")
        ttk.Entry(daily_row, textvariable=self.daily_start, width=12).pack(side="left")
        ttk.Label(daily_row, text="（空=今天）", foreground="#777").pack(side="left", padx=(4, 0))

        repeat_every = safe_int(t.get("daily_repeat_every"), 0)
        repeat_duration = safe_int(t.get("daily_repeat_duration"), 0)
        self.daily_repeat_enabled = tk.BooleanVar(value=repeat_every != 0)
        every_n, every_u = split_secs(repeat_every or 300)
        dur_n, dur_u = split_secs(repeat_duration if repeat_every else 3600)
        self.daily_every = tk.StringVar(value=str(every_n))
        self.daily_every_unit = tk.StringVar(value=every_u)
        self.daily_dur = tk.StringVar(value=str(dur_n))
        self.daily_dur_unit = tk.StringVar(value=dur_u)
        repeat_row = ttk.Frame(self.f_daily)
        repeat_row.pack(anchor="w", pady=(8, 0))
        ttk.Checkbutton(repeat_row, text="重复任务，每", variable=self.daily_repeat_enabled,
                        command=self._switch_daily_repeat).pack(side="left")
        every_entry = ttk.Spinbox(repeat_row, from_=1, to=999999, textvariable=self.daily_every, width=7)
        every_entry.pack(side="left", padx=2)
        every_unit = ttk.Combobox(repeat_row, textvariable=self.daily_every_unit,
                                  values=UNIT_ORDER, state="readonly", width=5)
        every_unit.pack(side="left")
        ttk.Label(repeat_row, text="运行一次，持续").pack(side="left", padx=(6, 0))
        dur_entry = ttk.Spinbox(repeat_row, from_=1, to=999999, textvariable=self.daily_dur, width=7)
        dur_entry.pack(side="left", padx=2)
        dur_unit = ttk.Combobox(repeat_row, textvariable=self.daily_dur_unit,
                                values=UNIT_ORDER, state="readonly", width=5)
        dur_unit.pack(side="left")
        self.daily_repeat_widgets = ((every_entry, "normal"), (every_unit, "readonly"),
                                     (dur_entry, "normal"), (dur_unit, "readonly"))
        ttk.Label(self.f_daily, text="未勾选时仅运行一次；可跨午夜，持续时间不超过 N 天；截止时刻可触发，运行中的任务不强停。",
                  foreground="#777", wraplength=590).pack(anchor="w", pady=(5, 0))
        self._switch_daily_repeat()

        # 间隔重复
        self.f_interval = ttk.Frame(self.trig_holder)
        istart = parse_dt(t.get("interval_start") or "")
        if istart is None:
            istart = datetime.now()
        self.iv_date = tk.StringVar(value=istart.strftime("%Y-%m-%d"))
        self.iv_time = tk.StringVar(value=istart.strftime("%H:%M:%S"))
        every_n, every_u = split_secs(t.get("interval_every") or 300)
        self.iv_every = tk.StringVar(value=str(max(1, every_n)))
        self.iv_every_unit = tk.StringVar(value=every_u)
        dur = int(t.get("interval_duration") or 0)
        if dur > 0:
            dur_n, dur_u = split_secs(dur)
            self.iv_dur = tk.StringVar(value=str(dur_n))
            self.iv_dur_unit = tk.StringVar(value=dur_u)
        else:
            self.iv_dur = tk.StringVar(value="0")
            self.iv_dur_unit = tk.StringVar(value="分钟")
        ttk.Label(self.f_interval, text="从").pack(side="left")
        ttk.Entry(self.f_interval, textvariable=self.iv_date, width=11).pack(side="left", padx=2)
        ttk.Entry(self.f_interval, textvariable=self.iv_time, width=9).pack(side="left", padx=(2, 8))
        ttk.Label(self.f_interval, text="起，每").pack(side="left")
        ttk.Spinbox(self.f_interval, from_=1, to=999999, textvariable=self.iv_every, width=7).pack(side="left", padx=2)
        ttk.Combobox(self.f_interval, textvariable=self.iv_every_unit, values=UNIT_ORDER,
                     state="readonly", width=5).pack(side="left", padx=(0, 2))
        ttk.Label(self.f_interval, text="运行一次；持续").pack(side="left")
        ttk.Spinbox(self.f_interval, from_=0, to=999999, textvariable=self.iv_dur, width=7).pack(side="left", padx=2)
        ttk.Combobox(self.f_interval, textvariable=self.iv_dur_unit, values=UNIT_ORDER,
                     state="readonly", width=5).pack(side="left", padx=(0, 2))
        ttk.Label(self.f_interval, text="（0 = 无限期）", foreground="#777").pack(side="left")

    def _switch_daily_repeat(self):
        for widget, enabled_state in self.daily_repeat_widgets:
            widget.configure(state=enabled_state if self.daily_repeat_enabled.get() else "disabled")

    def _switch_trigger(self):
        for f in (self.f_once, self.f_daily, self.f_interval):
            f.pack_forget()
        key = self._trigger_key()
        if key == "once":
            self.f_once.pack(anchor="w", pady=6)
        elif key == "daily":
            self.f_daily.pack(anchor="w", pady=6)
        else:
            self.f_interval.pack(anchor="w", pady=6)

    def _trigger_key(self):
        try:
            idx = TRIGGER_LABELS.index(self.var_trigger.get())
            return TRIGGER_KEYS[idx]
        except ValueError:
            return "daily"

    # ------------------------------------------------------------------

    _last_browse_dir = None

    def _browse_script(self):
        p = filedialog.askopenfilename(
            title="选择 Python 脚本", parent=self,
            initialdir=TaskDialog._last_browse_dir or os.path.expanduser("~"),
            filetypes=[("Python 脚本", "*.py *.pyw"), ("所有文件", "*.*")],
        )
        if p:
            TaskDialog._last_browse_dir = os.path.dirname(p)
            self.var_script.set(os.path.normpath(p))

    def _browse_workdir(self):
        p = filedialog.askdirectory(title="选择工作目录", parent=self)
        if p:
            self.var_workdir.set(os.path.normpath(p))

    # ------------------------------------------------------------------

    def _form_updates(self):
        """先完整解析表单，验证失败时不改动共享任务。"""
        def clock_value(value):
            value = value.strip()
            if not re.fullmatch(r"\d{1,2}(?::\d{1,2}){0,2}", value) or not hms_valid(value):
                raise ValueError("时间格式不正确（HH:MM:SS）。")
            return "%02d:%02d:%02d" % parse_hms(value)

        def date_time(date_var, time_var):
            date = parse_date(date_var.get().strip())
            if date is None:
                raise ValueError("日期格式不正确（YYYY-MM-DD）。")
            return "%s %s" % (date.isoformat(), clock_value(time_var.get()))

        def seconds(number_var, unit_var, label, minimum):
            unit = unit_var.get()
            if unit not in UNIT_SECONDS:
                raise ValueError("%s的单位无效。" % label)
            value = _schedule_integer(number_var.get(), label, minimum) * UNIT_SECONDS[unit]
            timedelta(seconds=value)
            return value

        key = self._trigger_key()
        updates = {
            "name": self.var_name.get().strip() or "未命名任务",
            "group": self.var_group.get().strip() or "默认分组",
            "script": self.var_script.get().strip(),
            "args": self.var_args.get().strip(),
            "workdir": self.var_workdir.get().strip(),
            "console": self.var_console.get(),
            "trigger_type": key,
            "timeout": _schedule_integer(self.var_timeout.get(), "超时限制"),
            "conflict": {v: k for k, v in CONFLICT_LABELS.items()}.get(self.var_conflict.get(), "skip_new"),
        }
        if key == "once":
            updates["once_datetime"] = date_time(self.once_date, self.once_time)
        elif key == "daily":
            updates["trigger_time"] = clock_value(self.daily_time.get())
            days = _schedule_integer(self.daily_n.get(), "每隔天数", 1)
            timedelta(days=days)
            sd = self.daily_start.get().strip()
            start_date = parse_date(sd) if sd else datetime.now().date()
            if start_date is None:
                raise ValueError("起始日期格式不正确（YYYY-MM-DD）。")
            every, duration = 0, 0
            if self.daily_repeat_enabled.get():
                every = seconds(self.daily_every, self.daily_every_unit, "重复间隔", 1)
                duration = seconds(self.daily_dur, self.daily_dur_unit, "重复持续时间", 1)
                every, duration = _daily_repeat_values(every, duration, days)
            updates.update(every_n_days=days, start_date=start_date.isoformat(),
                           daily_repeat_every=every, daily_repeat_duration=duration)
        else:
            updates["interval_start"] = date_time(self.iv_date, self.iv_time)
            updates["interval_every"] = seconds(self.iv_every, self.iv_every_unit, "重复间隔", 1)
            updates["interval_duration"] = seconds(self.iv_dur, self.iv_dur_unit, "重复持续时间", 0)
        return updates

    def _validate(self):
        if not self.var_script.get().strip():
            messagebox.showwarning(APP_NAME, "请填写脚本路径。", parent=self)
            return False
        sp = self.var_script.get().strip()
        if not os.path.isfile(sp):
            # 相对路径按工作目录解析后复查（与运行时行为一致）
            wd = self.var_workdir.get().strip()
            check = os.path.join(wd, sp) if (wd and not os.path.isabs(sp)) else sp
            if not os.path.isfile(check):
                if not messagebox.askyesno(APP_NAME, "脚本文件不存在：\n%s\n\n仍要保存吗？" % sp, parent=self):
                    return False
        try:
            self._validated_updates = self._form_updates()
        except ValueError as e:
            messagebox.showwarning(APP_NAME, str(e), parent=self)
            return False
        except OverflowError:
            messagebox.showwarning(APP_NAME, "调度间隔或持续时间超出可计算范围。", parent=self)
            return False
        return True

    def _on_save(self):
        if not self._validate():
            return
        updates = self._validated_updates
        schedule_fields = {"trigger_type", "once_datetime", "trigger_time", "every_n_days",
                           "start_date", "daily_repeat_every", "daily_repeat_duration",
                           "interval_start", "interval_every", "interval_duration"}
        try:
            with CFG_LOCK:
                t = self.task
                changed = any(t.get(k) != v for k, v in updates.items())
                if any(t.get(k) != v for k, v in updates.items() if k in schedule_fields):
                    draft = dict(t)
                    draft.update(updates, finished=False)
                    updates["finished"] = False
                    updates["next_run"] = iso(compute_next_run(draft, datetime.now()))
                # 调度线程的运行记录可能已更新，只写入表单字段及必要的下次运行时间。
                t.update(updates)
                master = self.__dict__.get("master")
                sched = getattr(master, "sched", None) if master is not None else None
                if changed and sched is not None:
                    sched._generations[t["id"]] = sched._generations.get(t["id"], 0) + 1
        except OverflowError:
            messagebox.showwarning(APP_NAME, "下次运行时间超出可计算范围，请缩短调度间隔。", parent=self)
            return
        self.ok = True
        self.destroy()


# ----------------------------------------------------------------------------
# GUI：设置对话框
# ----------------------------------------------------------------------------

MISSED_POLICY_LABELS = {"run_once": "周期任务迟到超过 5 分钟补跑一次", "skip": "周期任务迟到超过 5 分钟直接跳过"}


class SettingsDialog(tk.Toplevel):

    def __init__(self, master):
        super().__init__(master)
        self.ok = False
        self.title("全局设置")
        self.resizable(False, False)
        self.grab_set()
        self.transient(master)
        cfg = master.cfg
        s = cfg["settings"]

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        pad = {"padx": 6, "pady": 5}

        ttk.Label(body, text="Python 目录：").grid(row=0, column=0, sticky="e", **pad)
        f1 = ttk.Frame(body)
        f1.grid(row=0, column=1, sticky="we", **pad)
        py_init = s.get("python_dir") or ""
        if not py_init and not IS_FROZEN:
            # 脚本模式：程序本身就是 Python 在跑，直接预填当前解释器目录
            py_init = os.path.dirname(os.path.abspath(sys.executable))
        self.var_py = tk.StringVar(value=py_init)
        ttk.Entry(f1, textvariable=self.var_py, width=58).pack(side="left", fill="x", expand=True)
        ttk.Button(f1, text="浏览…", command=self._browse_py).pack(side="left", padx=(4, 0))
        row = 1
        ttk.Label(body, text="（必填：填 Python 安装目录或 python.exe 完整路径；", foreground="#777").grid(row=row, column=1, sticky="w")
        row += 1
        ttk.Label(body, text="不配置则任务无法运行）", foreground="#777").grid(row=row, column=1, sticky="w")
        row += 1

        ttk.Separator(body).grid(row=row, column=0, columnspan=2, sticky="ew", pady=6)
        row += 1

        ttk.Label(body, text="错过处理：").grid(row=row, column=0, sticky="e", **pad)
        mp = s.get("missed_policy", "run_once")
        self.var_missed = tk.StringVar(value=MISSED_POLICY_LABELS.get(mp, MISSED_POLICY_LABELS["run_once"]))
        ttk.Combobox(body, textvariable=self.var_missed, values=list(MISSED_POLICY_LABELS.values()),
                     state="readonly", width=32).grid(row=row, column=1, sticky="w", **pad)
        row += 1
        ttk.Label(body, text="记录保留天数：").grid(row=row, column=0, sticky="e", **pad)
        f3 = ttk.Frame(body)
        f3.grid(row=row, column=1, sticky="w", **pad)
        self.var_retain = tk.StringVar(value=str(s.get("log_retention_days", 7)))
        ttk.Spinbox(f3, from_=0, to=3650, textvariable=self.var_retain, width=8).pack(side="left")
        ttk.Label(f3, text=" 天（按天日志文件自动清理；0 = 永久保留）").pack(side="left")
        row += 1

        ttk.Separator(body).grid(row=row, column=0, columnspan=2, sticky="ew", pady=6)
        row += 1

        # 自启 / 看门狗
        self.var_autostart = tk.BooleanVar(value=autostart_state())
        ttk.Checkbutton(
            body, text="开机自动启动调度器（当前用户）", variable=self.var_autostart
        ).grid(row=row, column=0, columnspan=2, sticky="w", **pad)
        row += 1
        # 勾选框初值取用户配置而非系统实际注册状态：任务未注册时若读实际状态，
        # "打开设置→保存"会把 watchdog_enabled 意外重置为 false，看门狗从此不再自动注册
        self.var_watchdog = tk.BooleanVar(value=bool(s.get("watchdog_enabled", True)))
        ttk.Checkbutton(
            body, text="保持看门狗（程序启动时自动注册，取消勾选则不再自动拉起）",
            variable=self.var_watchdog,
        ).grid(row=row, column=0, columnspan=2, sticky="w", **pad)
        row += 1
        ttk.Label(body, text="（看门狗通过文件锁检测存活，调度器崩溃 / 被结束后自动重启；",
                  foreground="#777").grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        ttk.Label(body, text=("  依赖 watchdog.exe 与已配置的 Python 目录）" if IS_FROZEN
                             else "  依赖 watchdog.py 与已配置的 Python 目录）"),
                  foreground="#777").grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        bf = ttk.Frame(body)
        bf.grid(row=row, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(bf, text="保存", command=self._on_save).pack(side="left", padx=8)
        ttk.Button(bf, text="取消", command=self.destroy).pack(side="left", padx=8)
        center_over_parent(self, master)

    def _browse_py(self):
        initial = self.var_py.get().strip()
        if initial and os.path.isfile(initial):
            initial = os.path.dirname(initial)
        if not (initial and os.path.isdir(initial)):
            initial = os.path.expanduser("~")
        d = filedialog.askdirectory(title="选择 Python 目录（或选择 python.exe）", initialdir=initial, parent=self)
        if d:
            self.var_py.set(os.path.normpath(d))

    def _on_save(self):
        pydir = self.var_py.get().strip()
        if pydir:
            p = pydir
            if os.path.isdir(p):
                p = os.path.join(p, "python.exe")
            if not os.path.isfile(p):
                if not messagebox.askyesno(
                        APP_NAME,
                        "该路径下找不到 python.exe：\n%s\n\n仍要保存吗？" % p,
                        parent=self):
                    return
        s = self.master.cfg["settings"]
        s["python_dir"] = pydir
        rev = {v: k for k, v in MISSED_POLICY_LABELS.items()}
        s["missed_policy"] = rev.get(self.var_missed.get(), "run_once")
        try:
            s["log_retention_days"] = max(0, int(self.var_retain.get()))
        except (ValueError, TypeError):
            s["log_retention_days"] = 7

        # ---- 开机自启 / 看门狗
        problems = []
        want_auto = bool(self.var_autostart.get())
        want_wd = bool(self.var_watchdog.get())
        if IS_FROZEN:
            # exe 模式：直接注册 exe 自身路径
            auto_cmd = '"%s"' % MAIN_EXE
            wd_cmd = '"%s"' % WATCHDOG_EXE
            launch_err = None
        else:
            pyw, launch_err = resolve_launcher_python(self.master.cfg)
            auto_cmd = '"%s" "%s"' % (pyw, MAIN_SCRIPT)
            wd_cmd = '"%s" "%s"' % (pyw, WATCHDOG_SCRIPT)
        if (want_auto or want_wd) and launch_err:
            problems.append("开机自启 / 看门狗未开启：%s" % launch_err)
        else:
            if want_auto:
                try:
                    autostart_set(True, auto_cmd)
                except Exception as e:
                    problems.append("开机自启写入注册表失败：%s" % e)
            if want_wd:
                ok, out = watchdog_set(True, wd_cmd)
                if not ok:
                    problems.append("看门狗计划任务创建失败：%s" % (out[:200] or "未知错误"))
        if not want_auto and autostart_state():
            try:
                autostart_set(False)
            except Exception as e:
                problems.append("关闭开机自启失败：%s" % e)
        if not want_wd and watchdog_state():
            ok, out = watchdog_set(False)
            if not ok:
                problems.append("看门狗计划任务删除失败：%s" % (out[:200] or "未知错误"))

        # 记住看门狗开关：False = 下次启动不再自动注册
        s["watchdog_enabled"] = want_wd

        self.master.sched.mark_dirty()
        if not save_config(self.master.cfg):
            messagebox.showerror(APP_NAME, "配置保存失败，设置尚未落盘，请检查目录权限或磁盘空间后重试。", parent=self)
            return
        self.ok = True
        self.destroy()
        if problems:
            messagebox.showwarning(APP_NAME, "设置已保存，但部分自启项未生效：\n\n" + "\n".join(problems),
                                    parent=self.master)


# ----------------------------------------------------------------------------
# GUI：运行记录窗口
# ----------------------------------------------------------------------------

RUN_COLS = ("time", "task", "group", "planned", "start", "end", "dur", "result", "code", "note")
RUN_HEADS = {"time": "记录时间", "task": "任务", "group": "分组", "planned": "计划触发",
             "start": "开始", "end": "结束", "dur": "耗时(s)", "result": "结果",
             "code": "退出码", "note": "备注"}
MAX_RUN_ROWS = 5000


class RunsWindow(tk.Toplevel):

    def __init__(self, master, prefill_task=None):
        super().__init__(master)
        self.title("运行记录")
        self.geometry("1180x560")
        self.transient(master)

        top = ttk.Frame(self, padding=(8, 8, 8, 0))
        top.pack(fill="x")
        ttk.Label(top, text="任务：").pack(side="left")
        names = ["全部"]
        for t in master.cfg["tasks"]:
            nm = t.get("name") or t["id"]
            if nm not in names:
                names.append(nm)
        self.var_task = tk.StringVar(value=(prefill_task.get("name") if prefill_task else "全部"))
        self.var_task.trace_add("write", lambda *a: self._load())
        ttk.Combobox(top, textvariable=self.var_task, values=names, state="readonly",
                     width=18).pack(side="left", padx=(0, 8))
        ttk.Label(top, text="结果：").pack(side="left")
        self.var_status = tk.StringVar(value="全部")
        self.var_status.trace_add("write", lambda *a: self._load())
        ttk.Combobox(top, textvariable=self.var_status,
                     values=["全部", "成功", "失败", "超时结束", "被终止", "跳过", "错误"],
                     state="readonly", width=9).pack(side="left", padx=(0, 8))
        ttk.Label(top, text="时间：").pack(side="left")
        self.var_range = tk.StringVar(value="全部")
        self.var_range.trace_add("write", lambda *a: self._load())
        ttk.Combobox(top, textvariable=self.var_range,
                     values=["全部", "今天", "近 24 小时", "近 1 小时"],
                     state="readonly", width=9).pack(side="left", padx=(0, 8))
        self.var_auto = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="自动刷新", variable=self.var_auto,
                        command=self._auto_toggle).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="刷新", command=lambda: self._load()).pack(side="left", padx=4)
        ttk.Button(top, text="打开日志目录", command=self._open_logs).pack(side="left", padx=4)
        ttk.Button(top, text="清空记录", command=self._clear).pack(side="left", padx=4)

        wrap = ttk.Frame(self, padding=8)
        wrap.pack(fill="both", expand=True)
        cols = RUN_COLS
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings")
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        xsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        widths = {"time": 148, "task": 130, "group": 84, "planned": 148, "start": 148,
                  "end": 148, "dur": 64, "result": 72, "code": 56, "note": 240}
        for c in cols:
            self.tree.heading(c, text=RUN_HEADS[c])
            self.tree.column(c, width=widths.get(c, 90), anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.tree.tag_configure("bad", foreground="#c62828")
        self.tree.tag_configure("ok", foreground="#0a7d32")

        self.status = ttk.Label(self, anchor="w", padding=(10, 4))
        self.status.pack(fill="x")

        self._auto_job = None
        self._row_meta = {}
        self.tree.bind("<Double-1>", self._open_run_log)
        self.bind("<Destroy>", self._on_win_destroy, True)

        center_over_parent(self, master)
        self._load()

    # ------------------------------------------------------------------

    def _auto_toggle(self):
        if self._auto_job:
            try:
                self.after_cancel(self._auto_job)
            except Exception:
                pass
            self._auto_job = None
        if self.var_auto.get():
            self._auto_job = self.after(3000, self._auto_refresh)

    def _auto_refresh(self):
        self._auto_job = None
        if not self.var_auto.get():
            return
        try:
            self._load()
        except Exception:
            pass
        try:
            self._auto_job = self.after(3000, self._auto_refresh)
        except Exception:
            pass

    def _on_win_destroy(self, event):
        if event is not None and event.widget is not self:
            return
        if self._auto_job:
            try:
                self.after_cancel(self._auto_job)
            except Exception:
                pass
            self._auto_job = None

    def _open_run_log(self, event):
        """双击某条记录：直接打开该次运行的输出日志 run_任务ID_日期.log"""
        iid = self.tree.identify_row(event.y)
        if not iid or iid not in self._row_meta:
            return
        tid, day = self._row_meta[iid]
        if not tid or not day:
            return
        p = os.path.join(LOGS_DIR, "run_%s_%s.log" % (tid, day))
        if os.path.isfile(p):
            try:
                os.startfile(p)
            except Exception as e:
                messagebox.showerror(APP_NAME, str(e), parent=self)
        else:
            messagebox.showinfo(
                APP_NAME,
                "未找到该次运行的输出文件：\n%s\n\n（弹窗模式运行不产生输出日志）" % p, parent=self)

    def _open_logs(self):
        try:
            os.makedirs(LOGS_DIR, exist_ok=True)
            os.startfile(LOGS_DIR)
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e), parent=self)

    def _clear(self):
        if not messagebox.askyesno(APP_NAME, "确定清空全部运行记录（runs-*.jsonl 与按天日志）？", parent=self):
            return
        failed = []
        try:
            for fn in os.listdir(LOGS_DIR):
                full = os.path.join(LOGS_DIR, fn)
                if (fn.startswith("runs-") and fn.endswith(".jsonl")) or fn == "runs.jsonl":
                    try:
                        os.remove(full)
                    except OSError:
                        failed.append(fn)
        except OSError:
            pass
        try:
            if os.path.isdir(DAILY_LOG_DIR):
                for fn in os.listdir(DAILY_LOG_DIR):
                    if fn.endswith(".log"):
                        try:
                            os.remove(os.path.join(DAILY_LOG_DIR, fn))
                        except Exception:
                            pass
        except Exception:
            pass
        self._load()
        if failed:
            messagebox.showwarning(
                APP_NAME,
                "有 %d 个记录文件正被写入，未能删除（其余已清空）：\n%s" % (len(failed), "\n".join(failed[:5])),
                parent=self)

    def _load(self):
        self.tree.delete(*self.tree.get_children(""))
        self._row_meta = {}
        # 按天文件 新 → 旧 读取；边读边按 任务/结果/时间 筛选，命中满上限即停——
        # 高频任务一天几万条时，早上的失败不会被"最近 5000 条"截断在候选集外
        run_files = []
        try:
            for fn in os.listdir(LOGS_DIR):
                if fn.startswith("runs-") and fn.endswith(".jsonl"):
                    run_files.append(fn)
            run_files.sort(reverse=True)
        except OSError:
            pass
        if os.path.isfile(RUNS_LOG_PATH):
            run_files.append(os.path.basename(RUNS_LOG_PATH))
        name_f = self.var_task.get()
        status_f = self.var_status.get()
        range_f = self.var_range.get()
        now_dt = datetime.now()
        now_epoch = time.time()
        rows = []
        sequence = 0
        for fn in run_files:
            try:
                with open(os.path.join(LOGS_DIR, fn), "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(r, dict):
                            continue
                        if name_f != "全部" and (r.get("task_name") or "") != name_f:
                            continue
                        if status_f != "全部" and (r.get("status_cn") or "") != status_f:
                            continue
                        if range_f != "全部":
                            dt = parse_dt(r.get("start") or r.get("time") or "")
                            if dt is None:
                                continue
                            if range_f == "今天" and dt.date() != now_dt.date():
                                continue
                            eps = time.mktime(dt.timetuple())
                            if range_f == "近 24 小时" and now_epoch - eps > 86400:
                                continue
                            if range_f == "近 1 小时" and now_epoch - eps > 3600:
                                continue
                        sequence += 1
                        item = (str(r.get("time") or r.get("end") or r.get("start") or ""), sequence, r)
                        if len(rows) < MAX_RUN_ROWS:
                            heapq.heappush(rows, item)
                        elif item[:2] > rows[0][:2]:
                            heapq.heapreplace(rows, item)
            except OSError:
                continue
        rows = [item[2] for item in sorted(rows, reverse=True)]
        for r in rows:
            tags = ()
            st = r.get("status") or ""
            if st in ("failed", "timeout", "killed", "error"):
                tags = ("bad",)
            elif st == "success":
                tags = ("ok",)
            vals = (
                r.get("time") or "",
                r.get("task_name") or "",
                r.get("group") or "",
                r.get("planned") or "",
                r.get("start") or "",
                r.get("end") or "",
                r.get("duration") if r.get("duration") is not None else "",
                r.get("status_cn") or "",
                "" if r.get("exit_code") is None else r.get("exit_code"),
                r.get("note") or "",
            )
            iid = self.tree.insert("", "end", values=vals, tags=tags)
            self._row_meta[iid] = ((r.get("task_id") or ""),
                                   (r.get("start") or "")[:10].replace("-", ""))
        total = len(rows)
        self.status.config(text="共 %d 条记录%s  ｜  双击行可打开该次运行输出"
                           % (total, ("（仅显示最近 %d 条）" % MAX_RUN_ROWS) if total >= MAX_RUN_ROWS else ""))


# ----------------------------------------------------------------------------
# GUI：主窗口
# ----------------------------------------------------------------------------

TASK_COLS = ("status", "script", "trigger", "next", "last_result", "last_time")
TASK_HEADS = {"status": "状态", "script": "脚本", "trigger": "触发器",
              "next": "下次运行", "last_result": "上次结果", "last_time": "上次运行时间"}


class App(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1080x620")
        self.minsize(860, 460)

        self.cfg = load_config()
        self.sched = Scheduler(self.cfg)
        self._need_rebuild = True
        self._group_items = {}
        self._warned_python = False

        self._build_style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.sched.start()
        self.after(600, self._poll)
        # 程序启动时自动注册看门狗（设置里关掉后不再自动注册）
        if self.cfg["settings"].get("watchdog_enabled", True):
            threading.Thread(target=self._ensure_watchdog, daemon=True).start()
        if not self.cfg["settings"].get("python_dir"):
            self.after(300, self._warn_python)

    def _ensure_watchdog(self):
        """自动注册看门狗计划任务（已注册则不重复操作）"""
        try:
            if watchdog_state():
                return
            if IS_FROZEN:
                if os.path.isfile(WATCHDOG_EXE):
                    watchdog_set(True, '"%s"' % WATCHDOG_EXE)
            else:
                if not os.path.isfile(WATCHDOG_SCRIPT):
                    return
                pyw, err = resolve_launcher_python(self.cfg)
                if not err:
                    watchdog_set(True, '"%s" "%s"' % (pyw, WATCHDOG_SCRIPT))
        except Exception:
            _exhook(*sys.exc_info())

    # ---- UI 构建 ----------------------------------------------------------

    def _build_style(self):
        style = ttk.Style(self)
        for th in ("vista", "winnative", "clam"):
            if th in style.theme_names():
                try:
                    style.theme_use(th)
                except Exception:
                    pass
                break

    def _build_ui(self):
        # 工具栏
        bar = ttk.Frame(self, padding=(8, 6, 8, 2))
        bar.pack(fill="x")
        for text, cmd in (
            ("新增任务", self._add_task),
            ("编辑", self._edit_task),
            ("删除", self._delete_task),
            ("启用/停用", self._toggle_task),
            ("立即运行", self._run_now),
        ):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)
        ttk.Label(bar, text="  ").pack(side="left")
        for text, cmd in (
            ("导入XML", self._import_xml),
            ("导出XML", self._export_xml),
            ("运行记录", lambda: self._show_runs()),
            ("设置", self._show_settings),
        ):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)
        ttk.Label(bar, text="  搜索：").pack(side="left")
        self.var_search = tk.StringVar()
        self.var_search.trace_add("write", lambda *a: setattr(self, "_need_rebuild", True))
        ttk.Entry(bar, textvariable=self.var_search, width=16).pack(side="left")

        # 任务列表
        wrap = ttk.Frame(self, padding=(8, 2, 8, 2))
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=TASK_COLS, show="tree headings", selectmode="browse")
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        xsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.tree.heading("#0", text="任务 / 分组")
        self.tree.column("#0", width=250, minwidth=160, anchor="w")
        widths = {"status": 86, "script": 190, "trigger": 200,
                  "next": 150, "last_result": 120, "last_time": 150}
        for c in TASK_COLS:
            self.tree.heading(c, text=TASK_HEADS[c])
            self.tree.column(c, width=widths.get(c, 100), anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self.tree.tag_configure("running", foreground="#0a7d32")
        self.tree.tag_configure("disabled", foreground="#8a8a8a")
        self.tree.tag_configure("finished", foreground="#8a8a8a")
        self.tree.tag_configure("bad", foreground="#c62828")
        self.tree.tag_configure("warn", foreground="#b8860b")

        self.tree.bind("<Double-1>", self._on_tree_double)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Button-3>", self._popup_menu)

        # 状态栏
        self.statusbar = ttk.Label(self, anchor="w", padding=(10, 4), relief="sunken")
        self.statusbar.pack(fill="x", side="bottom")

    # ---- 数据 <-> 界面 -----------------------------------------------------

    def _task_by_id(self, tid):
        for t in self.cfg["tasks"]:
            if t.get("id") == tid:
                return t
        return None

    def _selected_task(self):
        sel = self.tree.selection()
        for iid in sel:
            t = self._task_by_id(iid)
            if t is not None:
                return t
        return None

    def _is_running(self, t):
        p = self.sched.running.get(t.get("id"))
        return p is not None and p.poll() is None

    def _row_values(self, t):
        if self._is_running(t):
            status = "● 运行中"
        elif t.get("finished"):
            status = "已完成"
        elif not t.get("enabled"):
            status = "已停用"
        else:
            status = "已启用"
        dur = t.get("last_run_duration")
        return (
            status,
            t.get("script") or "",
            trigger_desc(t),
            t.get("next_run") or ("—" if t.get("finished") else "未设置"),
            t.get("last_result") or "—",
            "%s（%.1fs）" % (t.get("last_run"), dur) if (t.get("last_run") and dur is not None) else (t.get("last_run") or "—"),
        )

    def _apply_tags(self, iid, t):
        tags = []
        if self._is_running(t):
            tags.append("running")
        elif t.get("finished"):
            tags.append("finished")
        elif not t.get("enabled"):
            tags.append("disabled")
        res = str(t.get("last_result") or "")
        if any(k in res for k in ("失败", "超时", "错误", "被终止")):
            tags.append("bad")
        elif "跳过" in res:
            tags.append("warn")   # 跳过用琥珀色：高频任务半跑半跳时一眼可见
        self.tree.item(iid, tags=tags)

    def _rebuild_tree(self):
        prev_open = set()
        for g, iid in self._group_items.items():
            if iid and self.tree.exists(iid) and self.tree.item(iid, "open"):
                prev_open.add(g)
        prev_sel = set(self.tree.selection())
        yview = self.tree.yview()
        self.tree.delete(*self.tree.get_children(""))
        self._group_items = {}
        kw = str(self.var_search.get() or "").strip().lower() if hasattr(self, "var_search") else ""
        groups = {}
        order = []
        for t in self.cfg["tasks"]:
            if kw:   # 搜索过滤：匹配任务名 / 脚本路径 / 分组（忽略大小写）
                hay = "%s %s %s" % (t.get("name") or "", t.get("script") or "", t.get("group") or "")
                if kw not in hay.lower():
                    continue
            g = t.get("group") or "默认分组"
            if g not in groups:
                groups[g] = []
                order.append(g)
            groups[g].append(t)
        for g in sorted(order):
            children = groups[g]
            gi = self.tree.insert(
                "", "end", text="▶ %s（%d）" % (g, len(children)),
                open=(g in prev_open or bool(kw)),
                values=("分组", "", "", "", "", ""),
            )
            self._group_items[g] = gi
            for t in children:
                self.tree.insert(gi, "end", iid=t["id"], text=t.get("name") or "未命名",
                                 values=self._row_values(t))
                self._apply_tags(t["id"], t)
        # 重建后恢复选中与滚动位置（批量整理任务时不丢上下文）
        sel_alive = [iid for iid in prev_sel if self.tree.exists(iid)]
        if sel_alive:
            self.tree.selection_set(sel_alive[0])
        try:
            self.tree.yview_moveto(yview[0])
        except Exception:
            pass

    def _poll(self):
        try:
            if self._need_rebuild:
                self._rebuild_tree()
                self._need_rebuild = False
            else:
                for t in self.cfg["tasks"]:
                    iid = t.get("id")
                    if iid and self.tree.exists(iid):
                        self.tree.item(iid, values=self._row_values(t))
                        self._apply_tags(iid, t)
            self._update_statusbar()
        except Exception:
            _exhook(*sys.exc_info())
        self.after(600, self._poll)

    def _update_statusbar(self):
        s = self.cfg["settings"]
        tasks = self.cfg["tasks"]
        enabled = [t for t in tasks if t.get("enabled")]
        running = [t for t in tasks if self._is_running(t)]
        py = s.get("python_dir") or "未配置"
        st = self.sched.day_stats
        bad = st.get("failed", 0) + st.get("timeout", 0) + st.get("error", 0)
        self.statusbar.config(
            text="Python：%s   |   任务：%d（启用 %d）   |   运行中：%d   |   今日：成功 %d / 失败 %d / 跳过 %d   |   调度器：运行中（精度：秒）"
                 % (py, len(tasks), len(enabled), len(running),
                    st.get("success", 0), bad, st.get("skipped", 0)),
            foreground=("#c62828" if bad > 0 else "#333333"),
        )

    # ---- 菜单 ------------------------------------------------------------

    def _on_tree_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            # 点击任务行以外的空白处取消选中
            self.tree.selection_set()
            return
        if iid in self._group_items.values():
            # 分组行任意位置点击切换展开/折叠；+/- 号（indicator）有原生行为，跳过避免二次切换
            el = ""
            try:
                el = self.tree.identify_element(event.x, event.y)
            except Exception:
                pass
            if el != "indicator":
                self.tree.item(iid, open=not self.tree.item(iid, "open"))

    def _on_tree_double(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and iid in self._group_items.values():
            return   # 分组行双击不当作"编辑任务"（展开/折叠由单击负责）
        self._edit_task()

    def _popup_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
        else:
            self.tree.selection_set()   # 右键空白处取消选中
        t = self._selected_task()
        group = None
        if t is None and iid and iid in self._group_items.values():
            group = next((g for g, gi in self._group_items.items() if gi == iid), None)
        menu = tk.Menu(self, tearoff=0)
        if t is not None:
            menu.add_command(label="立即运行", command=self._run_now)
            menu.add_command(label=("停用" if t.get("enabled") else "启用"), command=self._toggle_task)
            menu.add_separator()
            menu.add_command(label="编辑任务", command=self._edit_task)
            menu.add_command(label="复制任务", command=lambda: self._duplicate_task(t))
            menu.add_command(label="查看运行记录", command=lambda: self._show_runs(t))
            menu.add_command(label="打开运行输出（今日）", command=lambda: self._open_today_log(t))
            menu.add_command(label="导出XML…", command=lambda: self._export_xml(t))
            menu.add_separator()
            menu.add_command(label="删除任务", command=self._delete_task)
        elif group:
            menu.add_command(label="新增任务（分组：%s）" % group, command=lambda: self._add_task(group))
        else:
            menu.add_command(label="新增任务", command=self._add_task)
            menu.add_command(label="导入XML…", command=self._import_xml)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ---- 动作 ------------------------------------------------------------

    def _warn_python(self):
        if self._warned_python:
            return
        self._warned_python = True
        messagebox.showwarning(
            APP_NAME,
            "尚未配置 Python 目录，任务无法运行。\n\n请在弹出的设置中填写 Python 目录（必填）。",
            parent=self,
        )
        self._show_settings()

    def _show_settings(self):
        SettingsDialog(self)

    def _show_runs(self, task=None):
        RunsWindow(self, prefill_task=task)

    def _add_task(self, group=None):
        t = dict(TASK_DEFAULTS)
        t["id"] = uuid.uuid4().hex[:12]
        t["name"] = "新任务"
        groups = []
        for x in self.cfg["tasks"]:
            g = x.get("group") or "默认分组"
            if g not in groups:
                groups.append(g)
        if "默认分组" not in groups:
            groups.insert(0, "默认分组")
        if group:
            t["group"] = group
            if group not in groups:
                groups.insert(0, group)
        dlg = TaskDialog(self, t, groups)
        self.wait_window(dlg)
        if dlg.ok:
            with CFG_LOCK:
                self.cfg["tasks"].append(t)
                self.sched.mark_dirty()
                save_config(self.cfg)
            self._need_rebuild = True

    def _edit_task(self):
        t = self._selected_task()
        if t is None:
            messagebox.showinfo(APP_NAME, "请先选中一个任务。", parent=self)
            return
        groups = []
        for x in self.cfg["tasks"]:
            g = x.get("group") or "默认分组"
            if g not in groups:
                groups.append(g)
        dlg = TaskDialog(self, t, groups)
        self.wait_window(dlg)
        if dlg.ok:
            with CFG_LOCK:
                self.sched.mark_dirty()
                save_config(self.cfg)
            self._need_rebuild = True

    def _delete_task(self):
        t = self._selected_task()
        if t is None:
            messagebox.showinfo(APP_NAME, "请先选中一个任务。", parent=self)
            return
        if self._is_running(t):
            if not messagebox.askyesno(
                    APP_NAME,
                    "任务「%s」正在运行。\n删除后运行中的进程将被终止。确定删除？"
                    % t.get("name"), parent=self):
                return
        elif not messagebox.askyesno(APP_NAME, "确定删除任务「%s」？" % t.get("name"), parent=self):
            return
        if not self.sched.cancel_task(t["id"]):
            messagebox.showerror(APP_NAME, "未能确认任务进程已结束，已保留任务并停用后续调度。", parent=self)
            self._need_rebuild = True
            return
        with CFG_LOCK:
            if t in self.cfg["tasks"]:
                self.cfg["tasks"].remove(t)
            self.sched.running.pop(t["id"], None)
            self.sched.mark_dirty()
            save_config(self.cfg)
        self._need_rebuild = True

    def _toggle_task(self):
        t = self._selected_task()
        if t is None:
            messagebox.showinfo(APP_NAME, "请先选中一个任务。", parent=self)
            return
        with CFG_LOCK:
            t["enabled"] = not t.get("enabled")
            self.sched._generations[t["id"]] = self.sched._generations.get(t["id"], 0) + 1
            if t["enabled"]:
                self.sched._cancelled.discard(t["id"])
                if not t.get("finished"):
                    t["next_run"] = iso(compute_next_run(t, datetime.now()))
            self.sched.mark_dirty()
            save_config(self.cfg)
        self._need_rebuild = True

    def _duplicate_task(self, t):
        """复制任务：新 id、名称加" 副本"、其余字段原样，弹出编辑框可微调"""
        nt = dict(t)
        nt["id"] = uuid.uuid4().hex[:12]
        nt["name"] = (t.get("name") or "任务") + " 副本"
        nt.pop("active_run", None)
        nt.update(finished=False, last_run=None, last_result=None, last_run_duration=None)
        nt["next_run"] = iso(compute_next_run(nt, datetime.now()))
        groups = []
        for x in self.cfg["tasks"]:
            g = x.get("group") or "默认分组"
            if g not in groups:
                groups.append(g)
        dlg = TaskDialog(self, nt, groups)
        self.wait_window(dlg)
        if dlg.ok:
            with CFG_LOCK:
                self.cfg["tasks"].append(nt)
                self.sched.mark_dirty()
                save_config(self.cfg)
            self._need_rebuild = True

    def _open_today_log(self, t):
        p = os.path.join(LOGS_DIR, "run_%s_%s.log" % (t["id"], datetime.now().strftime("%Y%m%d")))
        if os.path.isfile(p):
            try:
                os.startfile(p)
            except Exception as e:
                messagebox.showerror(APP_NAME, str(e), parent=self)
        else:
            messagebox.showinfo(APP_NAME, "今日暂无运行输出（弹窗模式运行不产生输出文件）。", parent=self)

    def _run_now(self):
        t = self._selected_task()
        if t is None:
            messagebox.showinfo(APP_NAME, "请先选中一个任务。", parent=self)
            return
        self.sched._fire_async(t, datetime.now(), manual=True)

    # ---- XML 导入导出 -----------------------------------------------------

    def _import_xml(self):
        path = filedialog.askopenfilename(
            title="导入 Windows 任务计划程序 XML",
            parent=self,
            filetypes=[("任务 XML", "*.xml"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            t, pydir, warn = import_task_xml(path)
        except Exception as e:
            messagebox.showerror(APP_NAME, "导入失败：%s" % e, parent=self)
            return
        if t is None:
            messagebox.showwarning(APP_NAME, warn or "无法导入该 XML。", parent=self)
            return
        msgs = []
        if pydir and not str(self.cfg["settings"].get("python_dir") or "").strip():
            self.cfg["settings"]["python_dir"] = pydir
            msgs.append("已自动填入 Python 目录：%s" % pydir)
        if warn:
            msgs.append(warn)
        with CFG_LOCK:
            self.cfg["tasks"].append(t)
            self.sched.mark_dirty()
            save_config(self.cfg)
        self._need_rebuild = True
        messagebox.showinfo(
            APP_NAME,
            "已导入任务「%s」（分组：导入）%s" % (t["name"], ("\n\n" + "\n".join(msgs)) if msgs else ""),
            parent=self,
        )

    def _export_xml(self, task=None):
        if task is None:
            task = self._selected_task()
        if task is None:
            messagebox.showinfo(APP_NAME, "请先选中一个任务。", parent=self)
            return
        exe, err = self.sched.resolve_python()
        if err:
            if not messagebox.askyesno(
                    APP_NAME,
                    "%s\n\n导出的 XML 将使用 \"python.exe\" 作为命令（导入任务计划程序后请自行修改）。\n继续导出吗？"
                    % err, parent=self):
                return
            exe = "python.exe"
        fname = re.sub(r'[\\/:*?"<>|]', "_", task.get("name") or "task")
        path = filedialog.asksaveasfilename(
            title="导出任务 XML",
            parent=self,
            defaultextension=".xml",
            initialfile="%s.xml" % fname,
            filetypes=[("任务 XML", "*.xml")],
        )
        if not path:
            return
        try:
            export_task_xml(task, exe, path)
        except Exception as e:
            messagebox.showerror(APP_NAME, "导出失败：%s" % e, parent=self)
            return
        messagebox.showinfo(
            APP_NAME,
            "已导出：\n%s\n\n可在 Windows 任务计划程序中导入该文件。" % path,
            parent=self,
        )

    # ---- 关闭 ------------------------------------------------------------

    def _on_close(self):
        self.sched.set_closing(True)
        try:
            # 快照遍历：fire 工作线程可能并发写入 running，直接 items() 会 RuntimeError
            running = [tid for tid, p in list(self.sched.running.items()) if p.poll() is None]
            if running:
                r = messagebox.askyesnocancel(
                    APP_NAME,
                    "有 %d 个任务正在运行。\n\n【是】终止它们并退出\n【否】让子进程继续运行，仅退出调度器（失去超时控制）\n【取消】不退出" % len(running),
                    parent=self,
                )
                if r is None:
                    return
                if r:
                    for tid in running:
                        if not self.sched.kill_running(tid):
                            messagebox.showerror(APP_NAME, "任务进程未能结束，已取消退出。", parent=self)
                            return
            if watchdog_state():
                if messagebox.askyesno(
                        APP_NAME,
                        "检测到看门狗仍在运行。\n\n【是】退出并暂停看门狗拉起（下次启动程序时自动恢复）\n【否】仅退出（约 2 分钟内会被看门狗重新拉起）",
                        parent=self):
                    ok, detail = watchdog_set(False)
                    if not ok:
                        messagebox.showerror(APP_NAME, "暂停看门狗失败，已取消退出：\n" + detail, parent=self)
                        return
            if not save_config(self.cfg):
                messagebox.showerror(APP_NAME, "配置保存失败，已取消退出，请检查目录权限和磁盘空间。", parent=self)
                return
            self.sched.stop()
            self.destroy()
        except Exception:
            _exhook(*sys.exc_info())
            messagebox.showerror(APP_NAME, "退出准备失败，程序仍在运行，请查看错误日志。", parent=self)
            return
        finally:
            self.sched.set_closing(False)


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------

def _startup_alert(msg):
    """noconsole 模式下弹出致命错误提示"""
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, msg)
        root.destroy()
    except Exception:
        pass


def main():
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        os.makedirs(DAILY_LOG_DIR, exist_ok=True)
    except Exception as e:
        _startup_alert("无法创建日志目录（程序所在目录可能没有写权限）：\n%s\n\n%s" % (LOGS_DIR, e))
        return
    # 单实例锁：锁被占用说明调度器已在运行；进程退出（含崩溃/被杀）锁自动释放，
    # watchdog.py 依靠该锁判断存活，实现"挂掉后自动拉起"。
    global _LOCK_FILE
    try:
        _LOCK_FILE = open(LOCK_PATH, "a+b")
    except Exception as e:
        _startup_alert("无法打开锁文件：\n%s\n\n%s" % (LOCK_PATH, e))
        return
    _LOCK_FILE.seek(0)
    try:
        msvcrt.locking(_LOCK_FILE.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, "PyTask 调度器已在运行（检测到实例锁）。\n\n如需多开请勿，多个实例会重复触发任务。")
        return
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
