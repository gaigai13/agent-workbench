import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        app_module.DATABASE_PATH = root / "test.db"
        app_module.WORKSPACE_PATH = root / "workspace"
        app_module.APP_PASSWORD = "test-password-is-long"
        app_module.APP_SECRET = "test-secret-which-is-definitely-long-enough"
        app_module.CICADAS_API_KEY = "test-key"
        self.client = TestClient(app_module.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def test_authentication_required_and_login(self):
        self.assertEqual(self.client.get("/api/tasks").status_code, 401)
        response = self.client.post("/api/login", json={"password": "test-password-is-long"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/tasks").status_code, 200)

    def test_task_is_queued_then_completed_by_worker(self):
        self.client.post("/api/login", json={"password": "test-password-is-long"})

        async def fake_model(_messages, _timeout_seconds=100):
            return {"role": "assistant", "content": "任务完成：测试通过。"}

        with patch.object(app_module, "ask_model", fake_model):
            created = self.client.post("/api/tasks", json={"prompt": "运行一次任务测试"})
            self.assertEqual(created.status_code, 201)
            task_id = created.json()["id"]
            deadline = time.time() + 3
            status = "queued"
            while time.time() < deadline:
                detail = self.client.get(f"/api/tasks/{task_id}").json()
                status = detail["status"]
                if status in {"succeeded", "failed", "cancelled"}:
                    break
                time.sleep(0.05)
        self.assertEqual(status, "succeeded")
        self.assertEqual(detail["output"], "任务完成：测试通过。")

    def test_workspace_writes_are_bounded_and_confined(self):
        result = app_module.write_workspace_file("src/main.py", "print('你好')")
        self.assertTrue(result["ok"])
        self.assertEqual((app_module.WORKSPACE_PATH / "src/main.py").read_text(encoding="utf-8"), "print('你好')")
        with self.assertRaises(ValueError):
            app_module.write_workspace_file("../outside.txt", "no")
        with self.assertRaises(ValueError):
            app_module.write_workspace_file("too-large.txt", "x" * 40_001)


if __name__ == "__main__":
    unittest.main()
