import subprocess
import unittest

import PyTaskScheduler as app


class WindowsArgumentTests(unittest.TestCase):
    def test_roundtrip_windows_argument_values(self):
        cases = [
            ["--folder", "C:\\a b\\"],
            ["--message", 'say "hello"', ""],
            ["--output=C:\\my files\\done", " 中文内容 "],
            ["--json", '{"key": "some value"}'],
            ["one", "two\\", 'three\\"four'],
        ]
        for args in cases:
            with self.subTest(args=args):
                self.assertEqual(app.split_cmdline(subprocess.list2cmdline(args)), args)

    def test_xml_quote_uses_windows_escaping(self):
        for value in ["C:\\a b\\", 'a "quoted" value', "", " 中文 "]:
            with self.subTest(value=value):
                self.assertEqual(app._quote_arg(value), subprocess.list2cmdline([value]))


if __name__ == "__main__":
    unittest.main()
