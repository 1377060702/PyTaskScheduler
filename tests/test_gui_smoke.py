import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace

import PyTaskScheduler as app


class GuiSmokeTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.root.cfg = {"settings": {}, "tasks": []}
        self.root.sched = SimpleNamespace(_generations={})
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.script = Path(self.folder.name) / "sample.py"
        self.script.write_text("pass\n", encoding="utf-8")

    def test_daily_repeat_controls_save_and_reopen(self):
        task = dict(app.TASK_DEFAULTS, id="aabbccddeeff", script=str(self.script))
        dialog = app.TaskDialog(self.root, task, ["默认分组"])
        dialog.withdraw()
        self.assertFalse(dialog.daily_repeat_enabled.get())
        self.assertTrue(all(str(widget.cget("state")) == "disabled"
                            for widget, _ in dialog.daily_repeat_widgets))
        dialog.daily_repeat_enabled.set(True)
        dialog._switch_daily_repeat()
        dialog.daily_time.set("23:00:00")
        dialog.daily_n.set("3")
        dialog.daily_every.set("5")
        dialog.daily_every_unit.set("分钟")
        dialog.daily_dur.set("2")
        dialog.daily_dur_unit.set("小时")
        dialog._on_save()
        self.assertTrue(dialog.ok)
        self.assertEqual((task["daily_repeat_every"], task["daily_repeat_duration"]), (300, 7200))
        reopened = app.TaskDialog(self.root, task, ["默认分组"])
        reopened.withdraw()
        self.assertTrue(reopened.daily_repeat_enabled.get())
        self.assertEqual(reopened.daily_time.get(), "23:00:00")
        self.assertEqual(reopened.daily_n.get(), "3")
        self.assertLessEqual(reopened.winfo_reqwidth(), self.root.winfo_screenwidth())
        self.assertLessEqual(reopened.winfo_reqheight(), self.root.winfo_screenheight())
        reopened.destroy()

    def test_switching_trigger_types_keeps_correct_frame(self):
        task = dict(app.TASK_DEFAULTS, id="001122334455", script=str(self.script))
        dialog = app.TaskDialog(self.root, task, ["默认分组"])
        dialog.withdraw()
        for label, key, frame in zip(app.TRIGGER_LABELS, app.TRIGGER_KEYS,
                                     [dialog.f_once, dialog.f_daily, dialog.f_interval]):
            dialog.var_trigger.set(label)
            dialog._switch_trigger()
            self.assertEqual(dialog._trigger_key(), key)
            self.assertEqual(frame.winfo_manager(), "pack")
        dialog.destroy()


if __name__ == "__main__":
    unittest.main()
