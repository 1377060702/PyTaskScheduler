import ctypes
import subprocess
import sys
import threading
import unittest
from unittest import mock

import windows_process as processes


class ProcessIdentityTests(unittest.TestCase):
    def spawn(self, exit_code=37):
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import sys,time; sys.stdin.buffer.read(1); time.sleep(0.05); sys.exit(%d)" % exit_code],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        def cleanup():
            try:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)
            finally:
                proc.stdin.close()

        self.addCleanup(cleanup)
        return proc

    def attach(self, proc):
        identity = processes.process_identity(proc)
        self.assertIsInstance(identity, int)
        self.assertGreater(identity, 0)
        attached = processes.attach_process(proc.pid, identity)
        self.assertIsNotNone(attached)
        self.addCleanup(attached.close)
        self.assertEqual(attached.pid, proc.pid)
        return attached

    def release(self, proc):
        proc.stdin.write(b"x")
        proc.stdin.flush()

    def test_identity_uses_popen_handle_without_reopening_pid(self):
        proc = self.spawn()
        with mock.patch.object(processes._kernel32, "OpenProcess", side_effect=AssertionError("PID 不应重开")):
            self.assertEqual(processes.process_identity(proc), processes.process_identity(proc))

    def test_matching_identity_attaches_and_wrong_identity_is_rejected(self):
        proc = self.spawn()
        attached = self.attach(proc)
        self.assertIsNone(attached.poll())
        self.assertIsNone(processes.attach_process(proc.pid, processes.process_identity(proc) + 1))
        self.assertIsNone(proc.poll())

    def test_wait_timeout_then_natural_exit_and_idempotent_close(self):
        proc = self.spawn()
        attached = self.attach(proc)
        with self.assertRaises(subprocess.TimeoutExpired):
            attached.wait(timeout=0.01)
        self.release(proc)
        self.assertEqual(attached.wait(timeout=5), 37)
        self.assertEqual(attached.poll(), 37)
        self.assertEqual(attached.wait(), 37)
        attached.close()
        attached.close()
        self.assertEqual(attached.poll(), 37)
        self.assertEqual(attached.wait(timeout=0), 37)

    def test_closing_live_process_does_not_kill_it_and_rejects_queries(self):
        proc = self.spawn()
        attached = self.attach(proc)
        attached.close()
        self.assertIsNone(proc.poll())
        with self.assertRaises(ValueError):
            attached.poll()
        with self.assertRaises(ValueError):
            attached.wait(timeout=0)

    def test_confirmed_exit_supports_concurrent_wait_poll_and_close(self):
        proc = self.spawn()
        attached = self.attach(proc)
        self.release(proc)
        self.assertEqual(attached.wait(timeout=5), 37)
        ready = threading.Barrier(4)
        results = []
        errors = []

        def query(operation):
            try:
                ready.wait(timeout=2)
                results.append(operation())
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=query, args=(operation,))
                   for operation in (attached.poll, attached.wait, attached.close)]
        for thread in threads:
            thread.start()
        ready.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertCountEqual(results, [37, 37, None])
        self.assertEqual(attached.poll(), 37)
        self.assertEqual(attached.wait(), 37)

    def test_exited_process_is_not_attached(self):
        proc = self.spawn()
        identity = processes.process_identity(proc)
        self.release(proc)
        proc.wait(timeout=5)
        self.assertIsNone(processes.attach_process(proc.pid, identity))

    def test_exit_code_259_is_an_exit_not_still_running(self):
        proc = self.spawn(259)
        attached = self.attach(proc)
        self.release(proc)
        self.assertEqual(attached.wait(timeout=5), 259)
        self.assertEqual(attached.poll(), 259)

    def test_poll_remains_available_while_another_thread_waits(self):
        proc = self.spawn()
        attached = self.attach(proc)
        waiter_done = threading.Event()
        wait_started = threading.Event()
        poll_done = threading.Event()
        results = []

        def wait_for_exit():
            try:
                results.append(attached.wait(timeout=5))
            finally:
                waiter_done.set()

        original_wait = processes._kernel32.WaitForSingleObject

        def observed_wait(handle, milliseconds):
            if milliseconds:
                wait_started.set()
            return original_wait(handle, milliseconds)

        with mock.patch.object(processes._kernel32, "WaitForSingleObject", side_effect=observed_wait):
            waiter = threading.Thread(target=wait_for_exit)
            waiter.start()
            poller = threading.Thread(target=lambda: (attached.poll(), poll_done.set()))
            try:
                self.assertTrue(wait_started.wait(1))
                poller.start()
                self.assertTrue(poll_done.wait(1), "poll 不应等待子进程退出")
                self.assertFalse(waiter_done.is_set())
            finally:
                self.release(proc)
                waiter.join(timeout=5)
                if poller.ident is not None:
                    poller.join(timeout=5)
        self.assertEqual(results, [37])


class ProcessApiFailureTests(unittest.TestCase):
    def test_access_denied_is_raised_not_treated_as_exit(self):
        with mock.patch.object(processes._kernel32, "OpenProcess", return_value=None), \
                mock.patch.object(ctypes, "get_last_error", return_value=5):
            with self.assertRaises(OSError) as raised:
                processes.attach_process(123, 456)
        self.assertEqual(raised.exception.winerror, 5)

    def test_missing_pid_returns_none(self):
        with mock.patch.object(processes._kernel32, "OpenProcess", return_value=None), \
                mock.patch.object(ctypes, "get_last_error", return_value=87):
            self.assertIsNone(processes.attach_process(123, 456))

    def test_identity_query_error_closes_new_handle_and_is_raised(self):
        with mock.patch.object(processes._kernel32, "OpenProcess", return_value=123), \
                mock.patch.object(processes._kernel32, "GetProcessTimes", return_value=0), \
                mock.patch.object(processes._kernel32, "CloseHandle", return_value=1) as close, \
                mock.patch.object(ctypes, "get_last_error", return_value=5):
            with self.assertRaises(OSError) as raised:
                processes.attach_process(123, 456)
            close.assert_called_once_with(123)
        self.assertEqual(raised.exception.winerror, 5)

    def test_wait_and_exit_query_errors_are_raised(self):
        for wait_result, exit_result in ((0xFFFFFFFF, 1), (0, 0)):
            with self.subTest(wait_result=wait_result), \
                    mock.patch.object(processes._kernel32, "WaitForSingleObject", return_value=wait_result), \
                    mock.patch.object(processes._kernel32, "GetExitCodeProcess", return_value=exit_result), \
                    mock.patch.object(processes._kernel32, "CloseHandle", return_value=1), \
                    mock.patch.object(ctypes, "get_last_error", return_value=6):
                attached = processes.AttachedProcess(123, 456)
                try:
                    with self.assertRaises(OSError) as raised:
                        attached.poll()
                    self.assertEqual(raised.exception.winerror, 6)
                finally:
                    attached.close()

    def test_invalid_identity_input_does_not_open_process(self):
        with mock.patch.object(processes._kernel32, "OpenProcess") as opened:
            for pid, created in ((0, 1), (-1, 1), (True, 1), (2 ** 32, 1),
                                 (123, None), (123, 0), (123, "456"), (123, 2 ** 64)):
                with self.subTest(pid=pid, created=created), self.assertRaises(ValueError):
                    processes.attach_process(pid, created)
            opened.assert_not_called()

    def test_identity_mismatch_releases_handle_and_uses_limited_access(self):
        with mock.patch.object(processes._kernel32, "OpenProcess", return_value=123) as opened, \
                mock.patch.object(processes, "_creation_time", return_value=456), \
                mock.patch.object(processes._kernel32, "CloseHandle", return_value=1) as close:
            self.assertIsNone(processes.attach_process(789, 457))
            opened.assert_called_once_with(0x00100000 | 0x1000, False, 789)
            close.assert_called_once_with(123)

    def test_close_waits_for_active_wait_call(self):
        attached = processes.AttachedProcess(123, 456)
        wait_started = threading.Event()
        release_wait = threading.Event()
        close_started = threading.Event()
        closed = threading.Event()
        results = []

        def blocking_wait(handle, milliseconds):
            wait_started.set()
            release_wait.wait(5)
            return 0x102

        def wait_for_exit():
            try:
                attached.wait(timeout=1)
            except subprocess.TimeoutExpired:
                results.append("timeout")

        def close_handle():
            close_started.set()
            attached.close()
            closed.set()

        with mock.patch.object(processes._kernel32, "WaitForSingleObject", side_effect=blocking_wait), \
                mock.patch.object(processes._kernel32, "CloseHandle", return_value=1) as close:
            waiter = threading.Thread(target=wait_for_exit)
            closer = threading.Thread(target=close_handle)
            waiter.start()
            try:
                self.assertTrue(wait_started.wait(1))
                closer.start()
                self.assertTrue(close_started.wait(1))
                self.assertFalse(closed.wait(0.05))
                close.assert_not_called()
            finally:
                release_wait.set()
                waiter.join(timeout=5)
                if closer.ident is not None:
                    closer.join(timeout=5)
                attached.close()
            self.assertTrue(closed.is_set())
            self.assertEqual(results, ["timeout"])
            close.assert_called_once_with(456)

    def test_close_failure_is_raised_and_can_be_retried(self):
        attached = processes.AttachedProcess(123, 456)
        with mock.patch.object(processes._kernel32, "CloseHandle", side_effect=[0, 1]) as close, \
                mock.patch.object(ctypes, "get_last_error", return_value=6):
            with self.assertRaises(OSError) as raised:
                attached.close()
            self.assertEqual(raised.exception.winerror, 6)
            attached.close()
            attached.close()
            self.assertEqual(close.call_count, 2)


if __name__ == "__main__":
    unittest.main()
