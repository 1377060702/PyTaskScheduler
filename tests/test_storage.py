import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import PyTaskScheduler as app


class StorageTests(unittest.TestCase):
    def test_save_reports_success_and_flushes_to_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "config.json"
            with patch.object(app, "CONFIG_PATH", str(target)), \
                    patch.object(app.os, "fsync", wraps=app.os.fsync) as fsync:
                self.assertTrue(app.save_config({"tasks": [], "settings": {}}))
                fsync.assert_called_once()
            self.assertEqual(json.loads(target.read_text("utf-8"))["tasks"], [])

    def test_replace_failure_keeps_old_config_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "config.json"
            target.write_text('{"old": true}', encoding="utf-8")
            with patch.object(app, "CONFIG_PATH", str(target)), \
                    patch.object(app.os, "replace", side_effect=PermissionError("busy")), \
                    patch.object(app, "_exhook"):
                self.assertIs(app.save_config({"new": True}), False)
            self.assertEqual(json.loads(target.read_text("utf-8")), {"old": True})


if __name__ == "__main__":
    unittest.main()
