import os
import tempfile
import unittest
from pathlib import Path

import runner


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        runner.WORKSPACE = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def test_file_write_read_and_list(self):
        (runner.WORKSPACE / "src").mkdir()
        (runner.WORKSPACE / "src" / "main.py").write_text("print('你好')", encoding="utf-8")
        read = runner.run_action({"action": "read_file", "path": "src/main.py"})
        self.assertEqual(read["content"], "print('你好')")
        listed = runner.run_action({"action": "list_files", "path": "."})
        self.assertEqual(listed["files"], ["src/main.py"])

    def test_rejects_path_escape(self):
        with self.assertRaises(ValueError):
            runner.safe_path("../outside")
        with tempfile.TemporaryDirectory() as outside:
            link = runner.WORKSPACE / "escape"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("当前 Windows 环境不允许创建符号链接")
            with self.assertRaises(ValueError):
                runner.safe_path("escape/secret.txt")

    @unittest.skipUnless(os.name == "posix", "runner command is executed in a Linux container")
    def test_command_runs_in_workspace(self):
        result = runner.run_action({"action": "run_command", "command": "python -c \"print('ok')\""})
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("ok", result["stdout"])


if __name__ == "__main__":
    unittest.main()
