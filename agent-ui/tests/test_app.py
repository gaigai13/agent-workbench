import asyncio
import tempfile
import threading
import time
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

import app as app_module


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        app_module.DATABASE_PATH = root / "test.db"
        app_module.WORKSPACE_PATH = root / "workspace"
        app_module.CANDIDATE_PATH = root / "candidates"
        app_module.APP_PASSWORD = "test-password-is-long"
        app_module.APP_SECRET = "test-secret-which-is-definitely-long-enough"
        app_module.CICADAS_API_KEY = "test-key"
        app_module.TAVILY_API_KEY = ""
        app_module._login_attempts.clear()
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

        async def fake_model(_messages, _timeout_seconds=100, _effort="default"):
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
                if status in {"review", "succeeded", "failed", "cancelled"}:
                    break
                time.sleep(0.05)
        self.assertEqual(status, "review")
        self.assertEqual(detail["output"], "任务完成：测试通过。")
        self.assertEqual(detail["acceptance_criteria"], "按任务说明完成并验证。")
        self.assertEqual(detail["reasoning_effort"], "default")
        approved = self.client.post(f"/api/tasks/{task_id}/approve")
        self.assertEqual(approved.status_code, 200)
        approved_detail = self.client.get(f"/api/tasks/{task_id}").json()
        self.assertEqual(approved_detail["status"], "succeeded")
        self.assertEqual(approved_detail["events"][-1]["content"], "验收通过；候选改动已合入正式工作区，任务完成。")

    def test_request_changes_requeues_with_feedback_and_context(self):
        self.client.post("/api/login", json={"password": "test-password-is-long"})
        seen_messages = []

        async def fake_model(messages, _timeout_seconds=100, _effort="default"):
            seen_messages.append(messages)
            return {"role": "assistant", "content": "候选结果已提交。"}

        with patch.object(app_module, "ask_model", fake_model):
            created = self.client.post(
                "/api/tasks",
                json={
                    "goal": "为小游戏添加冲刺技能",
                    "acceptance_criteria": "按键触发；冷却可见；测试通过",
                    "context": "游戏使用原生 Canvas。",
                },
            )
            self.assertEqual(created.status_code, 201)
            task_id = created.json()["id"]
            deadline = time.time() + 3
            while time.time() < deadline:
                detail = self.client.get(f"/api/tasks/{task_id}").json()
                if detail["status"] == "review":
                    break
                time.sleep(0.05)
            self.assertEqual(detail["status"], "review")
            first_user_message = next(message["content"] for message in seen_messages[0] if message["role"] == "user")
            self.assertIn("按键触发", first_user_message)
            self.assertIn("原生 Canvas", first_user_message)
            candidate_file = app_module.candidate_workspace(task_id) / "kept-between-reviews.txt"
            app_module.write_workspace_file("kept-between-reviews.txt", "candidate progress", app_module.candidate_workspace(task_id))

            feedback = "补上手机触控，并在界面显示冷却剩余时间。"
            returned = self.client.post(f"/api/tasks/{task_id}/request-changes", json={"note": feedback})
            self.assertEqual(returned.status_code, 200)
            deadline = time.time() + 3
            while time.time() < deadline:
                detail = self.client.get(f"/api/tasks/{task_id}").json()
                if detail["status"] == "review" and len(seen_messages) >= 2:
                    break
                time.sleep(0.05)
            self.assertEqual(detail["status"], "review")
            second_user_message = next(message["content"] for message in seen_messages[1] if message["role"] == "user")
            self.assertIn(feedback, second_user_message)
            self.assertTrue(any(event["kind"] == "review" for event in detail["events"]))
            self.assertEqual(candidate_file.read_text(encoding="utf-8"), "candidate progress")
            self.assertFalse((app_module.WORKSPACE_PATH / "kept-between-reviews.txt").exists())
            contents = [event["content"] for event in detail["events"]]
            returned_at = contents.index(f"人工退回修改：{feedback}")
            requeued_at = contents.index("已重新加入队列，Agent 将根据退回意见继续处理。")
            started_at = contents.index("Agent 开始处理任务。", returned_at + 1)
            self.assertLess(returned_at, requeued_at)
            self.assertLess(requeued_at, started_at)
            self.assertEqual(self.client.post(f"/api/tasks/{task_id}/approve").status_code, 200)
            self.assertEqual((app_module.WORKSPACE_PATH / "kept-between-reviews.txt").read_text(encoding="utf-8"), "candidate progress")

    def test_successful_file_write_is_recorded_as_artifact(self):
        self.client.post("/api/login", json={"password": "test-password-is-long"})

        async def fake_model(messages, _timeout_seconds=100, _effort="default"):
            if not any(message.get("role") == "tool" for message in messages):
                return {
                    "role": "assistant",
                    "content": "正在写入文件。",
                    "tool_calls": [{
                        "id": "call-1",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": "src/game.js", "content": "// game"}),
                        },
                    }],
                }
            return {"role": "assistant", "content": "文件已写入，等待验收。"}

        with patch.object(app_module, "ask_model", fake_model):
            created = self.client.post(
                "/api/tasks",
                json={"goal": "实现游戏文件", "acceptance_criteria": "写入 src/game.js"},
            )
            task_id = created.json()["id"]
            deadline = time.time() + 3
            while time.time() < deadline:
                detail = self.client.get(f"/api/tasks/{task_id}").json()
                if detail["status"] == "review":
                    break
                time.sleep(0.05)
        self.assertEqual(detail["status"], "review")
        artifacts = [event["content"] for event in detail["events"] if event["kind"] == "artifact"]
        self.assertEqual(artifacts, ["候选工作区文件已修改（尚未合入）：src/game.js"])
        formal_file = app_module.WORKSPACE_PATH / "src/game.js"
        candidate_file = app_module.candidate_workspace(task_id) / "src/game.js"
        self.assertFalse(formal_file.exists())
        self.assertEqual(candidate_file.read_text(encoding="utf-8"), "// game")

        changes = self.client.get(f"/api/tasks/{task_id}/changes").json()["changes"]
        self.assertEqual([(item["path"], item["kind"]) for item in changes], [("src/game.js", "added")])
        self.assertIn("+// game", changes[0]["diff"])

        approved = self.client.post(f"/api/tasks/{task_id}/approve")
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(formal_file.read_text(encoding="utf-8"), "// game")
        self.assertFalse(app_module.candidate_task_root(task_id).exists())

    def test_large_candidate_diff_can_be_downloaded_in_full(self):
        self.client.post("/api/login", json={"password": "test-password-is-long"})
        content = "".join(f"line {index:05d} {'x' * 30}\n" for index in range(700))

        async def fake_model(_messages, _timeout_seconds=100, _effort="default"):
            if not any(message.get("role") == "tool" for message in _messages):
                return {
                    "role": "assistant",
                    "content": "写入较大的候选文件。",
                    "tool_calls": [{
                        "id": "call-large-diff",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": "large.txt", "content": content}),
                        },
                    }],
                }
            return {"role": "assistant", "content": "候选文件已写入，等待验收。"}

        with patch.object(app_module, "ask_model", fake_model):
            task_id = self.client.post("/api/tasks", json={"prompt": "大差异下载测试"}).json()["id"]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                detail = self.client.get(f"/api/tasks/{task_id}").json()
                if detail["status"] in {"review", "failed", "cancelled"}:
                    break
                time.sleep(0.05)

        self.assertEqual(detail["status"], "review")
        preview = self.client.get(f"/api/tasks/{task_id}/changes").json()["changes"][0]
        self.assertTrue(preview["truncated"])
        self.assertIn("差异过长，已截断", preview["diff"])

        full = self.client.get(
            f"/api/tasks/{task_id}/changes/full",
            params={"path": "large.txt"},
        )
        self.assertEqual(full.status_code, 200)
        self.assertIn("attachment", full.headers["content-disposition"])
        self.assertGreater(len(full.text), app_module.MAX_DIFF_CHARS)
        self.assertIn("+line 00000", full.text)
        self.assertIn("+line 00699", full.text)
        self.assertNotIn("差异过长，已截断", full.text)
        self.assertEqual(
            self.client.get(f"/api/tasks/{task_id}/changes/full", params={"path": "not-changed.txt"}).status_code,
            404,
        )
        self.assertEqual(self.client.post(f"/api/tasks/{task_id}/approve").status_code, 409)
        approved = self.client.post(
            f"/api/tasks/{task_id}/approve",
            json={"full_diff_reviewed": True},
        )
        self.assertEqual(approved.status_code, 200)
        detail = self.client.get(f"/api/tasks/{task_id}").json()
        self.assertIn("人工确认已检查完整差异。", [event["content"] for event in detail["events"]])
        self.assertEqual((app_module.WORKSPACE_PATH / "large.txt").read_text(encoding="utf-8"), content)

    def test_cancel_requested_before_review_transition_wins_race(self):
        self.client.post("/api/login", json={"password": "test-password-is-long"})

        async def fake_model(_messages, _timeout_seconds=100, _effort="default"):
            with app_module.db_connection() as db:
                db.execute("UPDATE tasks SET cancel_requested=1 WHERE status='running'")
            return {"role": "assistant", "content": "候选结果不应提交。"}

        with patch.object(app_module, "ask_model", fake_model), patch.object(app_module, "task_cancelled", return_value=False):
            created = self.client.post("/api/tasks", json={"prompt": "测试取消竞态"})
            task_id = created.json()["id"]
            deadline = time.time() + 3
            while time.time() < deadline:
                detail = self.client.get(f"/api/tasks/{task_id}").json()
                if detail["status"] in {"cancelled", "review", "failed"}:
                    break
                time.sleep(0.05)

        self.assertEqual(detail["status"], "cancelled")
        self.assertFalse(any(event["content"] == "Agent 已提交候选结果，等待人工验收。" for event in detail["events"]))

    def test_worker_waits_for_review_before_starting_next_task(self):
        self.client.post("/api/login", json={"password": "test-password-is-long"})
        seen = []

        async def fake_model(messages, _timeout_seconds=100, _effort="default"):
            prompt = next(message["content"] for message in messages if message["role"] == "user")
            seen.append(prompt)
            return {"role": "assistant", "content": "候选任务已完成。"}

        with patch.object(app_module, "ask_model", fake_model):
            first = self.client.post("/api/tasks", json={"prompt": "先完成第一项"}).json()["id"]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                first_detail = self.client.get(f"/api/tasks/{first}").json()
                if first_detail["status"] == "review":
                    break
                time.sleep(0.05)
            second = self.client.post("/api/tasks", json={"prompt": "再做第二项"}).json()["id"]
            time.sleep(0.2)
            second_detail = self.client.get(f"/api/tasks/{second}").json()
            self.assertEqual(second_detail["status"], "queued")
            self.assertEqual(len(seen), 1)

            self.assertEqual(self.client.post(f"/api/tasks/{first}/approve").status_code, 200)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                second_detail = self.client.get(f"/api/tasks/{second}").json()
                if second_detail["status"] == "review":
                    break
                time.sleep(0.05)

        self.assertEqual(second_detail["status"], "review")
        self.assertEqual(len(seen), 2)

    def test_formal_workspace_conflict_blocks_approval_without_overwriting(self):
        self.client.post("/api/login", json={"password": "test-password-is-long"})

        async def fake_model(messages, _timeout_seconds=100, _effort="default"):
            if not any(message.get("role") == "tool" for message in messages):
                return {
                    "role": "assistant",
                    "content": "写入候选文件。",
                    "tool_calls": [{
                        "id": "call-conflict",
                        "function": {"name": "write_file", "arguments": json.dumps({"path": "same.txt", "content": "agent"})},
                    }],
                }
            return {"role": "assistant", "content": "候选结果待验收。"}

        with patch.object(app_module, "ask_model", fake_model):
            created = self.client.post("/api/tasks", json={"prompt": "冲突保护测试"})
            task_id = created.json()["id"]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                detail = self.client.get(f"/api/tasks/{task_id}").json()
                if detail["status"] in {"review", "failed"}:
                    break
                time.sleep(0.05)

        self.assertEqual(detail["status"], "review")
        formal_file = app_module.WORKSPACE_PATH / "same.txt"
        formal_file.write_text("human edit", encoding="utf-8")
        approved = self.client.post(f"/api/tasks/{task_id}/approve")
        self.assertEqual(approved.status_code, 409)
        self.assertIn("正式工作区在任务期间发生变化", approved.json()["detail"])
        self.assertEqual(formal_file.read_text(encoding="utf-8"), "human edit")
        self.assertEqual(self.client.get(f"/api/tasks/{task_id}").json()["status"], "review")

    def test_failed_candidate_write_never_changes_formal_workspace(self):
        self.client.post("/api/login", json={"password": "test-password-is-long"})

        async def fake_model(messages, _timeout_seconds=100, _effort="default"):
            if not any(message.get("role") == "tool" for message in messages):
                return {
                    "role": "assistant",
                    "content": "写入后触发失败。",
                    "tool_calls": [{
                        "id": "call-fail",
                        "function": {"name": "write_file", "arguments": json.dumps({"path": "never-merged.txt", "content": "candidate"})},
                    }],
                }
            raise RuntimeError("模拟模型失败")

        with patch.object(app_module, "ask_model", fake_model):
            created = self.client.post("/api/tasks", json={"prompt": "失败隔离测试"})
            task_id = created.json()["id"]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                detail = self.client.get(f"/api/tasks/{task_id}").json()
                if detail["status"] in {"review", "failed"}:
                    break
                time.sleep(0.05)

        self.assertEqual(detail["status"], "failed")
        self.assertFalse((app_module.WORKSPACE_PATH / "never-merged.txt").exists())
        self.assertFalse(app_module.candidate_task_root(task_id).exists())

    def test_approved_apply_is_recoverable_after_partial_restart(self):
        app_module.WORKSPACE_PATH.mkdir(parents=True, exist_ok=True)
        app_module.init_db()
        now = int(time.time())
        with app_module.db_connection() as db:
            cursor = db.execute(
                "INSERT INTO tasks(title,prompt,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("恢复测试", "恢复候选合入", "applying", now, now),
            )
            task_id = int(cursor.lastrowid)
        app_module.ensure_candidate(task_id)
        app_module.write_workspace_file("recover.txt", "approved", app_module.candidate_workspace(task_id))
        app_module.apply_candidate_changes(task_id)
        # 模拟进程已写入正式文件、但尚未来得及更新 SQLite 的崩溃点。
        formal_file = app_module.WORKSPACE_PATH / "recover.txt"
        formal_file.write_text("approved", encoding="utf-8")
        app_module.recover_applying_tasks()
        self.assertEqual(formal_file.read_text(encoding="utf-8"), "approved")
        self.assertEqual(app_module.get_task(task_id)["status"], "succeeded")
        self.assertFalse(app_module.candidate_task_root(task_id).exists())

    def test_recovery_refuses_candidate_content_changed_after_approval(self):
        app_module.WORKSPACE_PATH.mkdir(parents=True, exist_ok=True)
        app_module.init_db()
        now = int(time.time())
        with app_module.db_connection() as db:
            cursor = db.execute(
                "INSERT INTO tasks(title,prompt,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("验收后候选篡改", "恢复时不得合入未验收内容", "applying", now, now),
            )
            task_id = int(cursor.lastrowid)
        app_module.ensure_candidate(task_id)
        candidate = app_module.candidate_workspace(task_id)
        app_module.write_workspace_file("approved.txt", "approved", candidate)
        app_module.apply_candidate_changes(task_id)

        app_module.write_workspace_file("approved.txt", "changed after approval", candidate)
        app_module.recover_applying_tasks()
        self.assertFalse((app_module.WORKSPACE_PATH / "approved.txt").exists())
        self.assertEqual(app_module.get_task(task_id)["status"], "applying")
        self.assertIn("验收后发生变化", app_module.get_task(task_id)["error"])

        app_module.write_workspace_file("approved.txt", "approved", candidate)
        baseline = app_module.candidate_task_root(task_id) / "baseline"
        app_module.write_workspace_file("approved.txt", "changed baseline", baseline)
        app_module.recover_applying_tasks()
        self.assertFalse((app_module.WORKSPACE_PATH / "approved.txt").exists())
        self.assertIn("验收后发生变化", app_module.get_task(task_id)["error"])

        (baseline / "approved.txt").unlink()
        app_module.recover_applying_tasks()
        self.assertEqual((app_module.WORKSPACE_PATH / "approved.txt").read_text(encoding="utf-8"), "approved")
        self.assertEqual(app_module.get_task(task_id)["status"], "succeeded")

    def test_applying_task_retries_transient_recovery_failure(self):
        app_module.WORKSPACE_PATH.mkdir(parents=True, exist_ok=True)
        app_module.CANDIDATE_PATH.mkdir(parents=True, exist_ok=True)
        app_module.init_db()
        now = int(time.time())
        with app_module.db_connection() as db:
            cursor = db.execute(
                "INSERT INTO tasks(title,prompt,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("重试测试", "恢复临时错误", "applying", now, now),
            )
            task_id = int(cursor.lastrowid)
        app_module.ensure_candidate(task_id)
        app_module.write_workspace_file("retry.txt", "approved", app_module.candidate_workspace(task_id))
        app_module.apply_candidate_changes(task_id)
        original_apply = app_module.apply_candidate_changes
        calls = 0

        def fail_once(target_id, *, recovering=False):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("模拟一次性磁盘错误")
            return original_apply(target_id, recovering=recovering)

        with patch.object(app_module, "apply_candidate_changes", side_effect=fail_once):
            app_module.recover_applying_tasks()
            self.assertEqual(app_module.get_task(task_id)["status"], "applying")
            self.assertIn("模拟一次性磁盘错误", app_module.get_task(task_id)["error"])
            app_module.recover_applying_tasks()

        self.assertEqual(calls, 2)
        self.assertEqual(app_module.get_task(task_id)["status"], "succeeded")
        self.assertEqual((app_module.WORKSPACE_PATH / "retry.txt").read_text(encoding="utf-8"), "approved")

    def test_workspace_writes_are_bounded_and_confined(self):
        result = app_module.write_workspace_file("src/main.py", "print('你好')")
        self.assertTrue(result["ok"])
        self.assertEqual((app_module.WORKSPACE_PATH / "src/main.py").read_text(encoding="utf-8"), "print('你好')")
        with self.assertRaises(ValueError):
            app_module.write_workspace_file("../outside.txt", "no")
        with self.assertRaises(ValueError):
            app_module.write_workspace_file("too-large.txt", "x" * 40_001)

    def test_new_task_requires_acceptance_criteria(self):
        self.client.post("/api/login", json={"password": "test-password-is-long"})
        response = self.client.post("/api/tasks", json={"goal": "实现冲刺"})
        self.assertEqual(response.status_code, 422)

    def test_invalid_reasoning_effort_is_rejected(self):
        self.client.post("/api/login", json={"password": "test-password-is-long"})
        response = self.client.post("/api/tasks", json={"prompt": "测试", "reasoning_effort": "invalid"})
        self.assertEqual(response.status_code, 422)

    def test_reasoning_effort_is_persisted_and_passed_on_single_timeout_retry(self):
        self.client.post("/api/login", json={"password": "test-password-is-long"})
        seen = []

        def handle(request):
            seen.append(json.loads(request.content).get("reasoning_effort"))
            if len(seen) == 1:
                raise httpx.ReadTimeout("simulated slow response", request=request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "测试通过。"}}]})

        client_class = httpx.AsyncClient
        with patch.object(app_module.httpx, "AsyncClient",
                          side_effect=lambda **kw: client_class(transport=httpx.MockTransport(handle), **kw)):
            created = self.client.post("/api/tasks", json={"prompt": "测试低档", "reasoning_effort": "low"})
            self.assertEqual(created.status_code, 201)
            task_id = created.json()["id"]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                detail = self.client.get(f"/api/tasks/{task_id}").json()
                if detail["status"] in {"review", "failed"}:
                    break
                time.sleep(0.05)
        self.assertEqual(seen, ["low", "low"])
        self.assertEqual(detail["reasoning_effort"], "low")
        self.assertEqual(detail["status"], "review")
        self.assertTrue(any("重试" in event["content"] for event in detail["events"]))

    def test_existing_database_gains_review_columns(self):
        original_path = app_module.DATABASE_PATH
        with tempfile.TemporaryDirectory() as temp:
            legacy_path = Path(temp) / "legacy.db"
            db = sqlite3.connect(legacy_path)
            try:
                db.executescript(
                    """
                    CREATE TABLE tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        prompt TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        output TEXT NOT NULL DEFAULT '',
                        error TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                        created_at INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        content TEXT NOT NULL
                    );
                    """
                )
            finally:
                db.close()
            app_module.DATABASE_PATH = legacy_path
            try:
                app_module.init_db()
                db = sqlite3.connect(legacy_path)
                try:
                    columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
                finally:
                    db.close()
                self.assertTrue({"acceptance_criteria", "context", "review_feedback", "reasoning_effort"}.issubset(columns))
            finally:
                app_module.DATABASE_PATH = original_path


    def test_upgrade_pause_resume_keeps_checkpoint_and_waits_for_manual_approval(self):
        self.client.post("/api/login", json={"password": "test-password-is-long"})
        app_module.WORKSPACE_PATH.mkdir(parents=True, exist_ok=True)
        release_first_model = threading.Event()
        seen_tools = []
        model_calls = []

        async def fake_model(messages, _timeout_seconds=100, _effort="default", *, tools=None, max_tokens=None):
            model_calls.append(messages)
            seen_tools.extend(tool["function"]["name"] for tool in tools or [])
            if len(model_calls) == 1:
                await asyncio.to_thread(release_first_model.wait, 3)
                return {
                    "role": "assistant",
                    "content": "已检查项目，选择一个小改进。",
                    "tool_calls": [{
                        "id": "write-1",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": json.dumps({"path": "upgrade.txt", "content": "candidate-only"})},
                    }],
                    "_usage": {"prompt_tokens": 100, "completion_tokens": 40},
                }
            return {"role": "assistant", "content": "改动已完成并验证；等待人工验收。", "_usage": {"prompt_tokens": 120, "completion_tokens": 50}}

        with patch.object(app_module, "TAVILY_API_KEY", "search-test-key"), patch.object(app_module, "ask_model", fake_model):
            created = self.client.post("/api/upgrades", json={"goal": "改善本地工作台的一项可靠性问题", "budget_tokens": 10000, "max_model_calls": 4})
            self.assertEqual(created.status_code, 201, created.text)
            task_id = created.json()["id"]
            deadline = time.time() + 3
            detail = None
            while time.time() < deadline:
                detail = self.client.get(f"/api/tasks/{task_id}").json()
                if detail.get("model_calls") == 1:
                    break
                time.sleep(0.03)
            self.assertEqual(detail["status"], "running")
            self.assertIn("web_search", seen_tools)
            self.assertIn("request_owner_approval", seen_tools)
            self.assertEqual(self.client.post(f"/api/tasks/{task_id}/pause").status_code, 200)
            release_first_model.set()
            deadline = time.time() + 3
            while time.time() < deadline:
                detail = self.client.get(f"/api/tasks/{task_id}").json()
                if detail["status"] == "paused":
                    break
                time.sleep(0.03)
            self.assertEqual(detail["status"], "paused")
            self.assertFalse((app_module.candidate_workspace(task_id) / "upgrade.txt").exists())
            checkpoint = app_module.load_upgrade_checkpoint(app_module.get_task(task_id))
            self.assertEqual(checkpoint["next_tool_index"], 0)
            self.assertEqual(checkpoint["pending_calls"][0]["function"]["name"], "write_file")
            self.assertFalse((app_module.WORKSPACE_PATH / "upgrade.txt").exists())

            resumed = self.client.post(f"/api/tasks/{task_id}/resume", json={})
            self.assertEqual(resumed.status_code, 200, resumed.text)
            deadline = time.time() + 10
            while time.time() < deadline:
                detail = self.client.get(f"/api/tasks/{task_id}").json()
                if detail["status"] in {"review", "failed", "no_change"}:
                    break
                time.sleep(0.03)
            self.assertEqual(detail["status"], "review", detail)
            self.assertEqual((app_module.candidate_workspace(task_id) / "upgrade.txt").read_text(encoding="utf-8"), "candidate-only")
            self.assertFalse((app_module.WORKSPACE_PATH / "upgrade.txt").exists())
            self.assertEqual(detail["upgrade"]["usage_quality"], "actual")
            self.assertGreater(detail["upgrade"]["budget_tokens_used"], 0)
            self.assertEqual(self.client.post(f"/api/tasks/{task_id}/approve", json={}).status_code, 200)
            self.assertEqual((app_module.WORKSPACE_PATH / "upgrade.txt").read_text(encoding="utf-8"), "candidate-only")

    def test_upgrade_creation_requires_independent_search_key(self):
        self.client.post("/api/login", json={"password": "test-password-is-long"})
        response = self.client.post("/api/upgrades", json={})
        self.assertEqual(response.status_code, 503)
        self.assertIn("TAVILY_API_KEY", response.json()["detail"])

    def test_web_search_saves_https_sources_without_exposing_search_key_in_request_body(self):
        app_module.WORKSPACE_PATH.mkdir(parents=True, exist_ok=True)
        app_module.init_db()
        with app_module.db_connection() as db:
            cursor = db.execute(
                "INSERT INTO tasks(title,prompt,status,created_at,updated_at,task_type,phase) VALUES(?,?,?,?,?,'upgrade','research')",
                ("search test", "test", "running", int(time.time()), int(time.time())),
            )
            task_id = int(cursor.lastrowid)
        original_client = httpx.AsyncClient
        requests = []

        def handler(request):
            requests.append(request)
            body = json.loads(request.content)
            self.assertNotIn("api_key", body)
            self.assertEqual(request.headers.get("authorization"), "Bearer test-search-key")
            return httpx.Response(200, json={"results": [
                {"title": "Official docs", "url": "https://docs.example.test/guide", "content": "Trusted-looking but unverified excerpt."},
                {"title": "Unsafe scheme", "url": "http://example.test/", "content": "must be dropped"},
            ]})

        with patch.object(app_module, "TAVILY_API_KEY", "test-search-key"), patch.object(
            app_module.httpx, "AsyncClient", side_effect=lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs)
        ):
            result = asyncio.run(app_module.web_search("official API guidance", task_id, 2))
        data = json.loads(result)
        self.assertTrue(data["ok"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["results"][0]["trust"], "untrusted_public_content")
        with app_module.db_connection() as db:
            sources = db.execute("SELECT url,content_hash FROM task_sources WHERE task_id=?", (task_id,)).fetchall()
            row = db.execute("SELECT search_calls FROM tasks WHERE id=?", (task_id,)).fetchone()
        self.assertEqual(row["search_calls"], 1)
        self.assertEqual(sources[0]["url"], "https://docs.example.test/guide")
        self.assertEqual(len(sources[0]["content_hash"]), 64)

    def test_running_upgrade_recovery_preserves_runner_operation_id(self):
        app_module.init_db()
        now = int(time.time())
        operation_id = "12345678-1234-4234-8234-123456789abc"
        checkpoint = {
            "schema_version": 1,
            "messages": [{"role": "system", "content": "test"}, {"role": "user", "content": "resume"}],
            "pending_calls": [], "next_tool_index": 0, "tool_call_count": 0,
            "runtime_used_seconds": 0, "active_started_at": now - 1,
            "inflight_model": False, "reserved_tokens": 0,
        }
        with app_module.db_connection() as db:
            cursor = db.execute(
                "INSERT INTO tasks(title,prompt,status,created_at,updated_at,task_type,phase,checkpoint_json,runner_operation_id) "
                "VALUES(?,?,?,?,?,'upgrade','testing',?,?)",
                ("runner recovery", "test", "running", now, now, json.dumps(checkpoint), operation_id),
            )
            task_id = int(cursor.lastrowid)

        app_module.init_db()
        recovered = app_module.get_task(task_id)
        self.assertEqual(recovered["status"], "paused")
        self.assertEqual(recovered["runner_operation_id"], operation_id)

    def test_worker_does_not_resume_until_stale_runner_operation_is_cancelled(self):
        app_module.init_db()
        now = int(time.time())
        operation_id = "12345678-1234-4234-8234-123456789abd"
        with app_module.db_connection() as db:
            db.execute(
                "INSERT INTO tasks(title,prompt,status,created_at,updated_at,task_type,runner_operation_id) "
                "VALUES(?,?,?,?,?,'upgrade',?)",
                ("queued recovery", "test", "queued", now, now, operation_id),
            )
        stop = asyncio.Event()
        runner_calls = []
        started_tasks = []

        async def fake_runner_call(payload):
            runner_calls.append(payload)
            stop.set()
            return {"ok": False, "error": "runner still unavailable"}

        async def fake_process_task(task_id):
            started_tasks.append(task_id)

        with patch.object(app_module, "runner_call", fake_runner_call), patch.object(app_module, "process_task", fake_process_task):
            asyncio.run(app_module.task_worker(stop))

        self.assertEqual(runner_calls, [{"action": "cancel_command", "operation_id": operation_id}])
        self.assertEqual(started_tasks, [])
        row = app_module.get_task(1)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["runner_operation_id"], operation_id)

    def test_stale_runner_operation_id_clears_after_runner_acknowledges_cancel(self):
        app_module.init_db()
        now = int(time.time())
        operation_id = "12345678-1234-4234-8234-123456789abe"
        with app_module.db_connection() as db:
            cursor = db.execute(
                "INSERT INTO tasks(title,prompt,status,created_at,updated_at,task_type,runner_operation_id) "
                "VALUES(?,?,?,?,?,'upgrade',?)",
                ("cancel recovery", "test", "paused", now, now, operation_id),
            )
            task_id = int(cursor.lastrowid)

        with patch.object(app_module, "runner_call", new=AsyncMock(return_value={"ok": True, "terminated": True, "cancel_requested": True})) as call:
            asyncio.run(app_module.cancel_stale_upgrade_runner_operations())

        call.assert_awaited_once_with({"action": "cancel_command", "operation_id": operation_id})
        self.assertEqual(app_module.get_task(task_id)["runner_operation_id"], "")

    def test_cancelled_runner_call_keeps_operation_id_for_restart_recovery(self):
        app_module.WORKSPACE_PATH.mkdir(parents=True, exist_ok=True)
        app_module.init_db()
        now = int(time.time())
        fingerprint = app_module.workspace_fingerprint()
        authority = {"version": 1, "budgets": {"max_model_calls": 4, "max_search_calls": 2, "max_tool_calls": 10, "max_runtime_seconds": 1800}}
        checkpoint = {
            "schema_version": 1,
            "messages": [{"role": "system", "content": "test"}, {"role": "user", "content": "test"}],
            "pending_calls": [{"id": "command-1", "function": {"name": "run_command", "arguments": json.dumps({"command": "sleep 30"})}}],
            "next_tool_index": 0, "tool_call_count": 0, "runtime_used_seconds": 0,
            "budget_tokens_used": 0, "inflight_model": False, "reserved_tokens": 0,
        }
        with app_module.db_connection() as db:
            cursor = db.execute(
                "INSERT INTO tasks(title,prompt,status,created_at,updated_at,task_type,phase,checkpoint_json,authority_snapshot_json,base_fingerprint,budget_limit_tokens) "
                "VALUES(?,?,?, ?,?,'upgrade','testing',?,?,?,10000)",
                ("cancel recovery", "test", "running", now, now, json.dumps(checkpoint), json.dumps(authority), fingerprint),
            )
            task_id = int(cursor.lastrowid)

        async def cancelled_invoke_tool(_name, _arguments, _remaining, current_task_id, **kwargs):
            self.assertEqual(current_task_id, task_id)
            operation_id = kwargs["operation_id"]
            self.assertTrue(operation_id)
            self.assertEqual(app_module.get_task(task_id)["runner_operation_id"], operation_id)
            raise asyncio.CancelledError()

        with patch.object(app_module, "invoke_tool", cancelled_invoke_tool):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(app_module.process_upgrade_task(task_id))

        self.assertTrue(app_module.get_task(task_id)["runner_operation_id"])

    def test_running_upgrade_recovers_to_waiting_owner_when_model_result_is_unknown(self):
        self.client.post("/api/login", json={"password": "test-password-is-long"})
        app_module.WORKSPACE_PATH.mkdir(parents=True, exist_ok=True)
        fingerprint = app_module.workspace_fingerprint()
        now = int(time.time())
        authority = {"version": 1, "budgets": {"max_model_calls": 4, "max_search_calls": 2, "max_tool_calls": 10, "max_runtime_seconds": 1800}}
        checkpoint = {
            "schema_version": 1,
            "messages": [{"role": "system", "content": "test"}, {"role": "user", "content": "resume"}],
            "pending_calls": [], "next_tool_index": 0, "tool_call_count": 0,
            "runtime_used_seconds": 4, "active_started_at": now - 2,
            "inflight_model": True, "reserved_tokens": 500,
            "model_calls": 1, "budget_tokens_used": 500,
        }
        with app_module.db_connection() as db:
            cursor = db.execute(
                "INSERT INTO tasks(title,prompt,status,created_at,updated_at,task_type,phase,checkpoint_json,authority_snapshot_json,base_fingerprint,budget_limit_tokens,model_calls,budget_tokens_used) VALUES(?,?,?,?,?,'upgrade','decision',?,?,?,?,1,500)",
                ("restart recovery", "test", "running", now, now, json.dumps(checkpoint), json.dumps(authority), fingerprint, 10000),
            )
            task_id = int(cursor.lastrowid)
        app_module.init_db()
        recovered = app_module.get_task(task_id)
        self.assertEqual(recovered["status"], "waiting_owner")
        self.assertEqual(recovered["unknown_model_call"], 1)
        self.assertEqual(self.client.post(f"/api/tasks/{task_id}/resume", json={}).status_code, 409)

        async def fake_model(messages, _timeout_seconds=100, _effort="default", *, tools=None, max_tokens=None):
            return {"role": "assistant", "content": "从检查点恢复，暂无改动。"}

        with patch.object(app_module, "ask_model", fake_model):
            retried = self.client.post(f"/api/tasks/{task_id}/resume", json={"confirm_unknown_model_call": True})
            self.assertEqual(retried.status_code, 200, retried.text)
            deadline = time.time() + 10
            while time.time() < deadline:
                detail = self.client.get(f"/api/tasks/{task_id}").json()
                if detail["status"] in {"no_change", "failed", "review"}:
                    break
                time.sleep(0.03)
        self.assertEqual(detail["status"], "no_change", detail)
        self.assertEqual(detail["upgrade"]["model_calls"], 2)



    def test_upgrade_candidate_excludes_and_denies_secret_files(self):
        self.client.post("/api/login", json={"password": "test-password-is-long"})
        app_module.WORKSPACE_PATH.mkdir(parents=True, exist_ok=True)
        sensitive_paths = [
            ".env", ".envrc", ".envrc.local", "id_ed25519", ".npmrc", ".git-credentials",
            ".aws/credentials", ".git/config", ".docker/config.json", ".kube/config",
            ".config/gcloud/application_default_credentials.json", "terraform.tfvars",
            "terraform.tfvars.json", "service-account.json", "service_account.json", "client_secret.json",
        ]
        for relative in sensitive_paths:
            path = app_module.WORKSPACE_PATH / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("must-not-copy", encoding="utf-8")
        (app_module.WORKSPACE_PATH / "src").mkdir()
        (app_module.WORKSPACE_PATH / "src" / "main.py").write_text("print('safe')", encoding="utf-8")
        app_module.init_db()
        with app_module.db_connection() as db:
            cursor = db.execute(
                "INSERT INTO tasks(title,prompt,status,created_at,updated_at,task_type,phase) VALUES(?,?,?,?,?,'upgrade','inspection')",
                ("secret scope", "test", "running", int(time.time()), int(time.time())),
            )
            task_id = int(cursor.lastrowid)
        candidate = app_module.ensure_candidate(task_id, exclude_sensitive=True)
        self.assertTrue(app_module.is_sensitive_workspace_path(r".config\gcloud\application_default_credentials.json"))
        for relative in sensitive_paths:
            with self.subTest(path=relative):
                self.assertFalse((candidate / relative).exists())
                denied_read = asyncio.run(app_module.invoke_tool("read_file", {"path": relative}, 5, task_id, upgrade=True))
                denied_write = asyncio.run(app_module.invoke_tool("write_file", {"path": relative, "content": "override"}, 5, task_id, upgrade=True))
                self.assertIn("禁止", json.loads(denied_read)["error"])
                self.assertIn("禁止", json.loads(denied_write)["error"])
        self.assertTrue((candidate / "src" / "main.py").exists())
        with patch.object(app_module, "runner_call", new=AsyncMock(return_value={"ok": True, "files": [*sensitive_paths, "src/main.py", "sub/credentials.json"]})):
            listed = json.loads(asyncio.run(app_module.invoke_tool("list_files", {"path": "."}, 5, task_id, upgrade=True)))
        self.assertEqual(listed["files"], ["src/main.py"])

    def test_owner_approval_is_exact_and_scoped_to_one_candidate_action(self):
        self.client.post("/api/login", json={"password": "test-password-is-long"})
        app_module.WORKSPACE_PATH.mkdir(parents=True, exist_ok=True)
        calls = []

        async def fake_model(messages, _timeout_seconds=100, _effort="default", *, tools=None, max_tokens=None):
            calls.append(messages)
            if len(calls) == 1:
                return {
                    "role": "assistant",
                    "content": "需要一个需批准的动作。",
                    "tool_calls": [{
                        "id": "approval-1", "type": "function",
                        "function": {"name": "request_owner_approval", "arguments": json.dumps({"action": "仅在候选区调整现有配置", "reason": "验证需要调整配置"})},
                    }],
                }
            return {"role": "assistant", "content": "已在原授权范围内完成并提交验收。"}

        with patch.object(app_module, "TAVILY_API_KEY", "test-search-key"), patch.object(app_module, "ask_model", fake_model):
            created = self.client.post("/api/upgrades", json={"goal": "验证授权边界", "budget_tokens": 100000, "max_model_calls": 4})
            task_id = created.json()["id"]
            deadline = time.time() + 3
            while time.time() < deadline:
                detail = self.client.get(f"/api/tasks/{task_id}").json()
                if detail["status"] == "waiting_owner":
                    break
                time.sleep(0.03)
            self.assertEqual(detail["status"], "waiting_owner", detail)
            self.assertEqual(detail["upgrade"]["owner_request"]["action"], "仅在候选区调整现有配置")
            wrong = self.client.post(f"/api/tasks/{task_id}/authorize", json={"action": "Push 到 GitHub"})
            self.assertEqual(wrong.status_code, 409)
            approved = self.client.post(f"/api/tasks/{task_id}/authorize", json={"action": "仅在候选区调整现有配置"})
            self.assertEqual(approved.status_code, 200, approved.text)
            deadline = time.time() + 3
            while time.time() < deadline:
                detail = self.client.get(f"/api/tasks/{task_id}").json()
                if detail["status"] in {"no_change", "review", "failed"}:
                    break
                time.sleep(0.03)
            self.assertEqual(detail["status"], "no_change", detail)
            resumed_user = [m["content"] for m in calls[1] if m["role"] == "user"][-1]
            self.assertIn("仅在候选区", resumed_user)
            self.assertIn("仍禁止读取密钥、合并、Push、部署", resumed_user)


if __name__ == "__main__":
    unittest.main()
