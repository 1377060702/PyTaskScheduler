#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyTaskScheduler 看门狗（watchdog）

由 Windows 计划任务每 2 分钟以 pythonw 运行一次：
  - 调度器持有 scheduler.lock 的文件锁 → 说明活着，静默退出
  - 锁空闲（调度器崩溃 / 被结束 / 未开机自启）→ 用当前解释器拉起调度器

主程序退出（含崩溃）时操作系统自动释放文件锁，因此本检测无残留、无竞态。
主程序启动时也用同一把锁做单实例保护，看门狗不会拉起第二个实例。
"""

import msvcrt
import os
import subprocess
import sys

# 打包成 exe 时 __file__ 指向临时解压目录，须用 sys.executable 定位程序根目录
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))

LOCK_PATH = os.path.join(BASE, "scheduler.lock")
MAIN_SCRIPT = os.path.join(BASE, "PyTaskScheduler.py")
MAIN_EXE = os.path.join(BASE, "PyTaskScheduler.exe")
CREATE_NO_WINDOW = 0x08000000


def scheduler_alive():
    """尝试对 scheduler.lock 加非阻塞锁：能加上 = 没有实例在跑"""
    try:
        f = open(LOCK_PATH, "a+b")
    except OSError:
        # 打不开就当作活着，避免异常情况下反复拉起
        return True
    try:
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            return False
        except OSError:
            return True
    finally:
        try:
            f.close()
        except Exception:
            pass


def main():
    if scheduler_alive():
        return
    try:
        if getattr(sys, "frozen", False):
            # exe 模式：拉起同目录的 PyTaskScheduler.exe（不存在则放弃，不抛异常）
            if not os.path.isfile(MAIN_EXE):
                return
            subprocess.Popen([MAIN_EXE], cwd=BASE,
                             stdin=subprocess.DEVNULL,
                             creationflags=CREATE_NO_WINDOW)
        else:
            # 脚本模式：用当前解释器（pythonw）拉起主程序脚本
            exe = sys.executable or "pythonw.exe"
            subprocess.Popen([exe, MAIN_SCRIPT], cwd=BASE,
                             stdin=subprocess.DEVNULL,
                             creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass


if __name__ == "__main__":
    main()
