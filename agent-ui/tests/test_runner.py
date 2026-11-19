import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import runner


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        runner.WORKSPACE = Path(self.temp.name).resolve()
        runner.CANDIDATE_ROOT = runner.WORKSPACE / "candidates"

    def tearDown(self):
        with runner.RUNNING_COMMANDS_LOCK:
            runner.RUNNING_COMMANDS.clear()
            runner.CANCELLED_COMMANDS.clear()
        self.temp.cleanup()

    def test_file_read_and_list_are_scoped_to_candidate(self):
        candidate = runner.CANDIDATE_ROOT / "1"
        candidate.mkdir(parents=True)
        (candidate / ".ready").write_text("ready\n", encoding="utf-8")
        project = candidate / "workspace"
        (project / "src").mkdir(parents=True)
        (project / "src" / "main.py").write_text("print('你好')", encoding="utf-8")
        (runner.WORKSPACE / "formal.txt").write_text("formal", encoding="utf-8")
        read = runner.run_action({"action": "read_file", "path": "src/main.py", "candidate_id": 1})
        self.assertEqual(read["content"], "print('你好')")
        listed = runner.run_action({"action": "list_files", "path": ".", "candidate_id": 1})
        self.assertEqual(listed["files"], ["src/main.py"])

    def test_candidate_id_scopes_reads_and_file_listing(self):
        (runner.WORKSPACE / "formal.txt").write_text("formal", encoding="utf-8")
        candidate = runner.CANDIDATE_ROOT / "17"
        candidate.mkdir(parents=True)
        (candidate / ".ready").write_text("ready\n", encoding="utf-8")
        candidate_workspace = candidate / "workspace"
        candidate_workspace.mkdir()
        (candidate_workspace / "candidate.txt").write_text("candidate", encoding="utf-8")

        result = runner.run_action({"action": "list_files", "path": ".", "candidate_id": 17})
        self.assertEqual(result["files"], ["candidate.txt"])
        read = runner.run_action({"action": "read_file", "path": "candidate.txt", "candidate_id": 17})
        self.assertEqual(read["content"], "candidate")
        with self.assertRaises(ValueError):
            runner.run_action({"action": "list_files", "candidate_id": "17"})

    def test_command_scratch_is_copied_from_candidate_not_formal_workspace(self):
        (runner.WORKSPACE / "formal.txt").write_text("formal", encoding="utf-8")
        candidate = runner.CANDIDATE_ROOT / "18"
        candidate.mkdir(parents=True)
        (candidate / ".ready").write_text("ready\n", encoding="utf-8")
        candidate_workspace = candidate / "workspace"
        candidate_workspace.mkdir()
        (candidate_workspace / "candidate.txt").write_text("candidate", encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp:
            scratch = Path(temp) / "scratch"
            runner.prepare_scratch(scratch, candidate_workspace)
            self.assertEqual((scratch / "candidate.txt").read_text(encoding="utf-8"), "candidate")
            self.assertFalse((scratch / "formal.txt").exists())

    def test_rejects_path_escape_and_requires_candidate_id(self):
        with self.assertRaises(ValueError):
            runner.safe_path("../outside")
        with self.assertRaises(ValueError):
            runner.run_action({"action": "list_files", "path": "."})
        with tempfile.TemporaryDirectory() as outside:
            link = runner.WORKSPACE / "escape"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("当前 Windows 环境不允许创建符号链接")
            with self.assertRaises(ValueError):
                runner.safe_path("escape/secret.txt")

    def test_cancellation_before_command_start_prevents_execution_after_long_delay(self):
        operation_id = "12345678-1234-4234-8234-123456789abc"
        other_operation_id = "12345678-1234-4234-8234-123456789abd"
        cancelled_at = time.monotonic()
        self.assertTrue(runner.cancel_running_command(operation_id)["terminated"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            tmpdir = root / "tmp"
            home.mkdir()
            tmpdir.mkdir()
            with mock.patch.object(runner.time, "monotonic", return_value=cancelled_at + 301):
                self.assertTrue(runner.cancel_running_command(other_operation_id)["terminated"])
                with mock.patch.object(runner.subprocess, "Popen") as spawn:
                    result = runner.run_bounded_command("touch should-not-exist", root, 5, home, tmpdir, operation_id)
        spawn.assert_not_called()
        self.assertTrue(result["cancelled"])
        self.assertTrue(result["terminated"])

    def test_cancel_with_no_registered_command_is_confirmed_and_blocks_late_start(self):
        operation_id = "12345678-1234-4234-8234-123456789abd"
        result = runner.cancel_running_command(operation_id)
        self.assertTrue(result["ok"])
        self.assertTrue(result["terminated"])
        self.assertFalse(result["running"])
        self.assertIn(operation_id, runner.CANCELLED_COMMANDS)

    def test_cancellation_tombstone_limit_fails_closed(self):
        operation_id = "12345678-1234-4234-8234-123456789abc"
        runner.CANCELLED_COMMANDS.add("12345678-1234-4234-8234-123456789abd")
        with mock.patch.object(runner, "MAX_CANCELLED_COMMANDS", 1):
            result = runner.cancel_running_command(operation_id)
        self.assertFalse(result["ok"])
        self.assertFalse(result["terminated"])
        self.assertNotIn(operation_id, runner.CANCELLED_COMMANDS)

    @unittest.skipUnless(os.name == "posix", "runner command is executed in a Linux container")
    def test_cancellation_racing_with_process_start_waits_for_registration(self):
        operation_id = "12345678-1234-4234-8234-123456789abe"
        process_starting = threading.Event()
        allow_process_start = threading.Event()
        cancel_results = []
        command_results = []
        real_popen = runner.subprocess.Popen

        def delayed_popen(*args, **kwargs):
            process_starting.set()
            if not allow_process_start.wait(timeout=5):
                raise TimeoutError("test did not release process start")
            return real_popen(*args, **kwargs)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            tmpdir = root / "tmp"
            home.mkdir()
            tmpdir.mkdir()
            root.chmod(0o755)

            def run_command():
                command_results.append(runner.run_bounded_command("sleep 30", root, 10, home, tmpdir, operation_id))

            def cancel_command():
                cancel_results.append(runner.cancel_running_command(operation_id))

            with mock.patch.object(runner.subprocess, "Popen", side_effect=delayed_popen):
                command_thread = threading.Thread(target=run_command)
                command_thread.start()
                self.assertTrue(process_starting.wait(timeout=5))
                cancel_thread = threading.Thread(target=cancel_command)
                cancel_thread.start()
                time.sleep(0.05)
                allow_process_start.set()
                cancel_thread.join(timeout=5)
                command_thread.join(timeout=5)

        self.assertFalse(cancel_thread.is_alive())
        self.assertFalse(command_thread.is_alive())
        self.assertEqual(len(cancel_results), 1)
        self.assertTrue(cancel_results[0]["terminated"], cancel_results[0])
        self.assertEqual(len(command_results), 1)
        self.assertTrue(command_results[0]["terminated"], command_results[0])

    @unittest.skipUnless(os.name == "posix", "runner command is executed in a Linux container")
    def test_command_runs_in_candidate_workspace(self):
        candidate = runner.CANDIDATE_ROOT / "19"
        candidate.mkdir(parents=True)
        (candidate / ".ready").write_text("ready\n", encoding="utf-8")
        (candidate / "workspace").mkdir()
        result = runner.run_action({"action": "run_command", "command": "python -c \"print('ok')\"", "candidate_id": 19})
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("ok", result["stdout"])


if __name__ == "__main__":
    unittest.main()
