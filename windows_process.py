"""按 PID 和创建 FILETIME 接管进程；不终止进程，也不保证任务恰好执行一次。

Popen 成功到身份记录落盘之间仍存在崩溃窗口，调用方需要明确处理该限制。
"""

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import math
import subprocess
import threading


_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102
_WAIT_FAILED = 0xFFFFFFFF
_INFINITE = 0xFFFFFFFF
_ERROR_INVALID_PARAMETER = 87

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_kernel32.GetProcessTimes.restype = wintypes.BOOL
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


def _creation_time(handle):
    times = [wintypes.FILETIME() for _ in range(4)]
    if not _kernel32.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime


def process_identity(proc):
    """从 Popen 持有的原始句柄读取创建时间，避免按 PID 重开时发生复用。"""
    return _creation_time(int(proc._handle))


def _wait_handle(handle, milliseconds):
    result = _kernel32.WaitForSingleObject(handle, milliseconds)
    if result == _WAIT_TIMEOUT:
        return None
    if result == _WAIT_FAILED:
        raise ctypes.WinError(ctypes.get_last_error())
    if result != _WAIT_OBJECT_0:
        raise OSError("进程等待返回了未知状态：%d" % result)
    # 已确认进程退出，退出码 259 也应作为实际退出码返回。
    code = wintypes.DWORD()
    if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
        raise ctypes.WinError(ctypes.get_last_error())
    return code.value


class AttachedProcess:
    """拥有一个已核对身份的进程句柄；使用完后由调用方 close()。"""

    def __init__(self, pid, handle):
        self.pid = pid
        self.returncode = None
        self._handle = handle
        self._condition = threading.Condition()
        self._users = 0
        self._closing = False

    @contextmanager
    def _borrow_handle(self):
        with self._condition:
            handle = None
            if self.returncode is None:
                if self._handle is None or self._closing:
                    raise ValueError("进程句柄已关闭")
                self._users += 1
                handle = self._handle
        try:
            yield handle
        finally:
            if handle is not None:
                with self._condition:
                    self._users -= 1
                    self._condition.notify_all()

    def _wait(self, milliseconds):
        with self._borrow_handle() as handle:
            if handle is None:
                return self.returncode
            result = _wait_handle(handle, milliseconds)
            if result is not None:
                with self._condition:
                    self.returncode = result
            return result

    def poll(self):
        return self._wait(0)

    def wait(self, timeout=None):
        milliseconds = _INFINITE if timeout is None else math.ceil(max(0, timeout) * 1000)
        if timeout is not None and milliseconds >= _INFINITE:
            raise OverflowError("等待超时值超过 Windows DWORD 毫秒范围")
        result = self._wait(milliseconds)
        if result is None:
            raise subprocess.TimeoutExpired("pid=%d" % self.pid, timeout)
        return result

    def close(self):
        with self._condition:
            while self._closing:
                self._condition.wait()
            if self._handle is None:
                return
            self._closing = True
            try:
                # 等待已有调用结束，不能关闭仍被 WaitForSingleObject 使用的句柄。
                while self._users:
                    self._condition.wait()
                if not _kernel32.CloseHandle(self._handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                self._handle = None
            finally:
                self._closing = False
                self._condition.notify_all()


def attach_process(pid, created):
    """接管仍运行且创建时间一致的进程；权限/API 错误抛出，不能当成已退出。"""
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        raise ValueError("PID 必须是有效的正 DWORD 整数")
    if type(created) is not int or not 0 < created <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("创建 FILETIME 必须是有效的正 64 位整数")
    handle = _kernel32.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == _ERROR_INVALID_PARAMETER:
            return None
        raise ctypes.WinError(error)
    attached = AttachedProcess(pid, handle)
    try:
        if _creation_time(handle) == created and attached.poll() is None:
            return attached
    except BaseException:
        attached.close()
        raise
    attached.close()
    return None
