import copy
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET


_sys_hook, _thread_hook = sys.excepthook, threading.excepthook
import PyTaskScheduler as app
sys.excepthook, threading.excepthook = _sys_hook, _thread_hook


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class XmlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "task.xml"

    def task(self, **values):
        task = dict(app.TASK_DEFAULTS)
        task.update(id="xml-test", script=r"C:\Jobs\daily.py", name="每日 & 检查",
                    start_date="2026-09-23", trigger_time="23:30:00")
        task.update(values)
        return task

    def write_xml(self, trigger, settings=""):
        self.path.write_text(
            '<Task xmlns="%s"><Triggers>%s</Triggers><Settings>%s</Settings>'
            '<Actions><Exec><Command>C:\\Python\\python.exe</Command>'
            '<Arguments>C:\\Jobs\\daily.py</Arguments></Exec></Actions></Task>'
            % (app.TS_NS, trigger, settings), encoding="utf-8")

    @staticmethod
    def daily(repetition="", days="2", boundary="2026-09-23T23:30:00", extra=""):
        return ("<CalendarTrigger><StartBoundary>%s</StartBoundary>%s%s"
                "<ScheduleByDay><DaysInterval>%s</DaysInterval></ScheduleByDay>"
                "</CalendarTrigger>") % (boundary, repetition, extra, days)

    @staticmethod
    def repetition(interval="PT5M", duration="PT2H", stop="false"):
        dur = "<Duration>%s</Duration>" % duration if duration is not None else ""
        return ("<Repetition><Interval>%s</Interval>%s"
                "<StopAtDurationEnd>%s</StopAtDurationEnd></Repetition>") % (interval, dur, stop)

    def test_daily_repetition_round_trip_preserves_calendar_schedule(self):
        for days in (1, 3):
            with self.subTest(days=days):
                original = self.task(every_n_days=days, daily_repeat_every=300,
                                     daily_repeat_duration=7200)
                app.export_task_xml(original, r"C:\Python\python.exe", self.path)
                result, _, warning = app.import_task_xml(self.path)
                self.assertIsNotNone(result, warning)
                for key in ("trigger_type", "every_n_days", "trigger_time", "start_date",
                            "daily_repeat_every", "daily_repeat_duration", "name"):
                    self.assertEqual(result.get(key), original[key], key)
                self.assertIsNone(warning)
                trigger = ET.parse(self.path).find(".//" + app._q("CalendarTrigger"))
                self.assertEqual([child.tag.split("}")[-1] for child in trigger],
                                 ["Enabled", "StartBoundary", "Repetition", "ScheduleByDay"])
                self.assertEqual(trigger.findtext(app._q("Repetition") + "/" +
                                                  app._q("StopAtDurationEnd")), "false")

    def test_daily_without_repetition_keeps_once_per_day(self):
        app.export_task_xml(self.task(), "python.exe", self.path)
        result, _, warning = app.import_task_xml(self.path)
        self.assertIsNotNone(result, warning)
        self.assertEqual(result.get("daily_repeat_every"), 0)
        self.assertEqual(result["trigger_type"], "daily")
        self.assertIsNone(ET.parse(self.path).find(".//" + app._q("Repetition")))

    def test_once_and_interval_round_trip_remain_distinct(self):
        for kind in ("once", "interval"):
            with self.subTest(kind=kind):
                task = self.task(trigger_type=kind, once_datetime="2026-10-01 09:00:00",
                                 interval_start="2026-10-01 09:00:00", interval_every=90,
                                 interval_duration=3600)
                app.export_task_xml(task, "python.exe", self.path)
                result, _, warning = app.import_task_xml(self.path)
                self.assertIsNotNone(result, warning)
                self.assertEqual(result["trigger_type"], kind)
                if kind == "interval":
                    self.assertEqual(result["interval_every"], 90)
                    self.assertEqual(result["interval_duration"], 3600)
                    self.assertEqual(ET.parse(self.path).findtext(".//" + app._q("StopAtDurationEnd")), "false")

    def test_import_rejects_unrepresentable_triggers(self):
        repetitions = self.repetition()
        cases = {
            "多触发器": self.daily() + self.daily(),
            "周触发器": self.daily().replace("ScheduleByDay", "ScheduleByWeek"),
            "带时区": self.daily(boundary="2026-09-23T23:30:00+08:00"),
            "UTC": self.daily(boundary="2026-09-23T23:30:00Z"),
            "非整数秒": self.daily(boundary="2026-09-23T23:30:00.500"),
            "没有日期": self.daily(boundary=""),
            "无限日重复": self.daily(self.repetition(duration=None)),
            "无效持续": self.daily(self.repetition(duration="invalid")),
            "小数重复": self.daily(self.repetition(interval="PT60.5S")),
            "零间隔": self.daily(self.repetition(interval="PT0S")),
            "超过窗口": self.daily(self.repetition(interval="PT3H")),
            "跨越下个周期": self.daily(self.repetition(duration="P3D")),
            "结束强停": self.daily(self.repetition(stop="true")),
            "无效天数": self.daily(days="x"),
            "触发截止": self.daily(extra="<EndBoundary>2026-10-01T00:00:00</EndBoundary>"),
            "随机延迟": self.daily(extra="<RandomDelay>PT5M</RandomDelay>"),
            "启动触发器": "<BootTrigger>%s</BootTrigger>" % repetitions,
        }
        for label, trigger in cases.items():
            with self.subTest(label=label):
                self.write_xml(trigger)
                result, _, warning = app.import_task_xml(self.path)
                self.assertIsNone(result)
                self.assertTrue(warning)

    def test_disabled_trigger_does_not_become_enabled(self):
        self.write_xml(self.daily(extra="<Enabled>false</Enabled>"))
        result, _, warning = app.import_task_xml(self.path)
        self.assertIsNotNone(result, warning)
        self.assertFalse(result["enabled"])

    def test_export_rejects_invalid_xml_ranges_before_writing(self):
        for values in ({"daily_repeat_every": 30, "daily_repeat_duration": 3600},
                       {"daily_repeat_every": 300, "daily_repeat_duration": 0},
                       {"daily_repeat_every": 300, "daily_repeat_duration": 86401},
                       {"every_n_days": 366}, {"every_n_days": "bad"},
                       {"trigger_time": "25:00"}, {"start_date": "bad"}):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    app.export_task_xml(self.task(**values), "python.exe", self.path)
                self.assertFalse(self.path.exists())

    def test_export_has_no_dangling_principal_reference(self):
        app.export_task_xml(self.task(), "python.exe", self.path)
        self.assertNotIn("Context", ET.parse(self.path).find(app._q("Actions")).attrib)

    def test_import_rejects_unsupported_or_invalid_instance_settings(self):
        for settings in ("<MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>",
                         "<MultipleInstancesPolicy>Queue</MultipleInstancesPolicy>",
                         "<MultipleInstancesPolicy>unknown</MultipleInstancesPolicy>",
                         "<Enabled>maybe</Enabled>",
                         "<ExecutionTimeLimit>bad</ExecutionTimeLimit>",
                         "<ExecutionTimeLimit>PT0.5S</ExecutionTimeLimit>"):
            with self.subTest(settings=settings):
                self.write_xml(self.daily(), settings)
                result, _, warning = app.import_task_xml(self.path)
                self.assertIsNone(result)
                self.assertTrue(warning)

    def test_import_uses_windows_default_timeout_and_explicit_no_timeout(self):
        for settings, timeout in (("", 72 * 3600), ("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", 0)):
            with self.subTest(settings=settings):
                self.write_xml(self.daily(), settings)
                result, _, warning = app.import_task_xml(self.path)
                self.assertIsNotNone(result, warning)
                self.assertEqual(result["timeout"], timeout)


class DialogTests(unittest.TestCase):
    def dialog(self, **overrides):
        dialog = app.TaskDialog.__new__(app.TaskDialog)
        dialog.task = dict(app.TASK_DEFAULTS)
        dialog.task.update(id="dialog-test", script="existing.py", start_date="2026-09-23",
                           last_run="2026-09-22 09:00:00", last_result="success")
        fields = dict(var_name="任务", var_group="分组", var_script="existing.py", var_args="",
                      var_workdir="", var_console="nowindow", var_trigger=app.TRIGGER_LABELS[1],
                      var_timeout="0", var_conflict=app.CONFLICT_LABELS["skip_new"],
                      daily_time="09:00:00", daily_n="1", daily_start="2026-09-23",
                      daily_repeat_enabled=True, daily_every="5", daily_every_unit="分钟",
                      daily_dur="2", daily_dur_unit="小时", once_date="2026-10-01",
                      once_time="09:00:00", iv_date="2026-10-01", iv_time="09:00:00",
                      iv_every="5", iv_every_unit="分钟", iv_dur="0", iv_dur_unit="分钟")
        fields.update(overrides)
        for name, value in fields.items():
            setattr(dialog, name, Value(value))
        dialog.ok = False
        dialog.destroy = Mock()
        return dialog

    def save(self, dialog):
        with patch.object(app.os.path, "isfile", return_value=True), \
                patch.object(app.messagebox, "showwarning") as warning, \
                patch.object(app, "save_config") as save_config:
            dialog._on_save()
            save_config.assert_not_called()
            return warning

    def test_save_repeat_and_normalize_date_without_running_task(self):
        dialog = self.dialog(daily_start="2026/09/23", daily_n="3")
        before_history = dialog.task["last_run"], dialog.task["last_result"]
        warning = self.save(dialog)
        warning.assert_not_called()
        self.assertTrue(dialog.ok)
        self.assertEqual(dialog.task["daily_repeat_every"], 300)
        self.assertEqual(dialog.task["daily_repeat_duration"], 7200)
        self.assertEqual(dialog.task["every_n_days"], 3)
        self.assertEqual(dialog.task["start_date"], "2026-09-23")
        self.assertEqual((dialog.task["last_run"], dialog.task["last_result"]), before_history)

    def test_blank_start_date_is_fixed_at_save(self):
        dialog = self.dialog(daily_start="")
        self.save(dialog)
        self.assertEqual(dialog.task["start_date"], datetime.now().strftime("%Y-%m-%d"))

    def test_disabling_repetition_clears_repeat_fields(self):
        dialog = self.dialog(daily_repeat_enabled=False, daily_every="bad", daily_dur="bad")
        dialog.task.update(daily_repeat_every=300, daily_repeat_duration=3600)
        self.save(dialog)
        self.assertTrue(dialog.ok)
        self.assertEqual(dialog.task["daily_repeat_every"], 0)
        self.assertEqual(dialog.task["daily_repeat_duration"], 0)

    def test_invalid_numbers_leave_entire_task_unchanged(self):
        for override in ({"daily_n": "bad"}, {"daily_n": "0"}, {"daily_n": "1.5"},
                         {"daily_n": "999999999999999999999"},
                         {"daily_every": "0"}, {"daily_every": "2.5"},
                         {"daily_dur": "-1"}, {"daily_dur": "0"},
                         {"daily_dur": "25"}, {"daily_every": "121"},
                         {"daily_every_unit": "天"}, {"var_timeout": "oops"},
                         {"var_timeout": "-1"}, {"daily_time": "09::30"},
                         {"var_trigger": app.TRIGGER_LABELS[2], "iv_dur": "oops"},
                         {"var_trigger": app.TRIGGER_LABELS[2], "iv_every": "999999999999999999999"}):
            with self.subTest(override=override):
                dialog = self.dialog(**override)
                before = copy.deepcopy(dialog.task)
                warning = self.save(dialog)
                self.assertFalse(dialog.ok)
                self.assertEqual(dialog.task, before)
                warning.assert_called_once()
                dialog.destroy.assert_not_called()

    def test_editing_metadata_preserves_finished_and_next_run(self):
        dialog = self.dialog(daily_repeat_enabled=False)
        dialog.task.update(daily_repeat_every=0, daily_repeat_duration=0,
                           finished=True, next_run=None)
        self.save(dialog)
        self.assertTrue(dialog.task["finished"])
        self.assertIsNone(dialog.task["next_run"])

    def test_save_updates_task_under_config_lock(self):
        dialog = self.dialog()
        held = []
        updates = []

        class Lock:
            def __enter__(self):
                held.append(True)

            def __exit__(self, *args):
                held.pop()

        class GuardedDict(dict):
            def update(self, *args, **kwargs):
                if not held:
                    raise AssertionError("任务更新必须持有 CFG_LOCK")
                updates.append(True)
                return super().update(*args, **kwargs)

            def __setitem__(self, key, value):
                if not held:
                    raise AssertionError("任务更新必须持有 CFG_LOCK")
                return super().__setitem__(key, value)

        dialog.task = GuardedDict(dialog.task)
        with patch.object(app, "CFG_LOCK", Lock()):
            self.save(dialog)
        self.assertTrue(dialog.ok)
        self.assertEqual(len(updates), 1)

    def test_save_invalidates_old_launch_generation_and_keeps_runtime_history(self):
        dialog = self.dialog(daily_time="10:00:00")
        generations = {dialog.task["id"]: 3}
        dialog.master = SimpleNamespace(sched=SimpleNamespace(_generations=generations))

        class Lock:
            def __enter__(self):
                dialog.task.update(last_run="2026-09-23 09:00:00", last_result="running",
                                   last_run_duration=13)

            def __exit__(self, *args):
                pass

        with patch.object(app, "CFG_LOCK", Lock()):
            self.save(dialog)
        self.assertEqual(generations[dialog.task["id"]], 4)
        self.assertEqual(dialog.task["last_run"], "2026-09-23 09:00:00")
        self.assertEqual(dialog.task["last_result"], "running")
        self.assertEqual(dialog.task["last_run_duration"], 13)

    def test_saving_unchanged_form_does_not_invalidate_generation(self):
        dialog = self.dialog()
        dialog.task.update(dialog._form_updates())
        generations = {dialog.task["id"]: 3}
        dialog.master = SimpleNamespace(sched=SimpleNamespace(_generations=generations))
        self.save(dialog)
        self.assertEqual(generations[dialog.task["id"]], 3)


if __name__ == "__main__":
    unittest.main()
