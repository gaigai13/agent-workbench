from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import tempfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "./data/agent.db"))
WORKSPACE_PATH = Path(os.getenv("WORKSPACE_PATH", "./workspace"))
RUNNER_SOCKET = os.getenv("RUNNER_SOCKET", "/run/agent/runner.sock")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
APP_SECRET = os.getenv("APP_SECRET", "")
API_BASE_URL = os.getenv("API_BASE_URL", "https://api.cicadas.top/v1").rstrip("/")
CICADAS_API_KEY = os.getenv("CICADAS_API_KEY", "")
MODEL = os.getenv("MODEL", "gpt-6.1-sol")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
COOKIE_NAME = "agent_session"
SESSION_SECONDS = 12 * 60 * 60
MAX_STEPS = 18
MAX_TASK_SECONDS = 30 * 60
MAX_CALLS_PER_STEP = 8
MAX_TOOL_CALLS = 36
MAX_PROMPT_CHARS = 8000
MAX_FILE_WRITE_BYTES = 40_000
MAX_WORKSPACE_BYTES = 128 * 1024 * 1024
MAX_QUEUED_TASKS = 20
MAX_RETAINED_TASKS = 200
MAX_EVENTS_PER_TASK = 100
MAX_EVENT_CHARS = 4000
MAX_MODEL_RESPONSE_BYTES = 2 * 1024 * 1024

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出项目工作区中的文件，path 必须是相对路径。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取工作区内的 UTF-8 文件。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "在工作区创建或覆盖 UTF-8 文件；必要时创建父目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "在隔离的项目容器中运行 Bash 命令；工作目录仅能是工作区内相对路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "workdir": {"type": "string", "default": "."},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
]

SYSTEM_PROMPT = """你是一个在隔离项目工作区中工作的中文编程 Agent。先查看现有文件，再按用户目标小步实施；尽量运行相关测试并核对结果。只能通过提供的工具读写工作区、执行命令。命令会在工作区临时副本中运行，生成的文件不会保存；要保留代码改动，使用 write_file。执行命令的容器没有网络访问，且不包含 API 密钥或宿主机目录。不要声称未验证的功能已完成。达到目标或无法继续时，用简体中文简要说明改动、验证情况和剩余事项。"""


def connect_db() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


@contextmanager
def db_connection():
    db = connect_db()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    with db_connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
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
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_task_id_id ON events(task_id, id);
            """
        )
        db.execute(
            "UPDATE tasks SET status='failed', error='服务重启时任务中断；工作区文件已保留，请重新提交任务。', updated_at=? WHERE status='running'",
            (int(time.time()),),
        )


def add_event(task_id: int, kind: str, content: str) -> None:
    now = int(time.time())
    with db_connection() as db:
        db.execute(
            "INSERT INTO events(task_id, created_at, kind, content) VALUES(?,?,?,?)",
            (task_id, now, kind[:40], content[:MAX_EVENT_CHARS]),
        )
        db.execute(
            "DELETE FROM events WHERE task_id=? AND id NOT IN (SELECT id FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?)",
            (task_id, task_id, MAX_EVENTS_PER_TASK),
        )
        db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (now, task_id))


def make_session(expiry: int) -> str:
    signature = hmac.new(APP_SECRET.encode(), str(expiry).encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{signature}"


def valid_session(token: str | None) -> bool:
    if not token or not APP_SECRET:
        return False
    try:
        expiry_text, supplied = token.split(".", 1)
        expiry = int(expiry_text)
    except (ValueError, TypeError):
        return False
    if expiry < int(time.time()):
        return False
    expected = hmac.new(APP_SECRET.encode(), expiry_text.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied, expected)


def safe_workspace_path(relative: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("路径不能为空")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("只允许工作区内相对路径")
    resolved = (WORKSPACE_PATH / path).resolve(strict=False)
    if not resolved.is_relative_to(WORKSPACE_PATH.resolve()):
        raise ValueError("路径不能越出工作区")
    return resolved


def workspace_size() -> int:
    total = 0
    for current, dirs, files in os.walk(WORKSPACE_PATH, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
        for name in files:
            path = Path(current) / name
            if not path.is_symlink() and path.is_file():
                total += path.stat().st_size
    return total


def write_workspace_file(relative: str, content: str) -> dict[str, Any]:
    path = safe_workspace_path(relative)
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_FILE_WRITE_BYTES:
        raise ValueError("单个文件不能超过 40 KB")
    old_size = path.stat().st_size if path.is_file() else 0
    if path.exists() and not path.is_file():
        raise ValueError("目标路径不是文件")
    if workspace_size() - old_size + len(encoded) > MAX_WORKSPACE_BYTES:
        raise ValueError("工作区达到 128 MB 上限")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_bytes(encoded)
        os.chmod(temporary, 0o640)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"ok": True, "written": path.relative_to(WORKSPACE_PATH.resolve()).as_posix(), "bytes": len(encoded)}


def require_auth(request: Request) -> None:
    if not valid_session(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="请先登录")


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class NewTaskBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)


_login_attempts: dict[str, list[float]] = {}


def _login_allowed(client: str) -> bool:
    now = time.monotonic()
    attempts = [t for t in _login_attempts.get(client, []) if now - t < 60]
    _login_attempts[client] = attempts
    if len(attempts) >= 8:
        return False
    attempts.append(now)
    return True


async def runner_call(payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode() + b"\n"

    def call() -> dict[str, Any]:
        import socket

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(150)
            client.connect(RUNNER_SOCKET)
            client.sendall(data)
            chunks = bytearray()
            while len(chunks) <= 100_000:
                part = client.recv(8192)
                if not part:
                    break
                chunks.extend(part)
                if b"\n" in part:
                    break
            if len(chunks) > 100_000:
                raise RuntimeError("隔离执行器响应过大")
            line = bytes(chunks).split(b"\n", 1)[0]
            return json.loads(line)

    try:
        return await asyncio.to_thread(call)
    except (OSError, TimeoutError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"隔离执行器暂不可用：{type(exc).__name__}"}


async def invoke_tool(name: str, arguments: dict[str, Any], remaining_seconds: float) -> str:
    if name == "write_file":
        content = arguments.get("content", "")
        path = arguments.get("path", "")
        if not isinstance(content, str) or not isinstance(path, str):
            return json.dumps({"ok": False, "error": "路径与文件内容必须是字符串"}, ensure_ascii=False)
        try:
            result = await asyncio.to_thread(write_workspace_file, path, content)
        except (OSError, ValueError) as exc:
            result = {"ok": False, "error": str(exc)}
        return json.dumps(result, ensure_ascii=False)
    if name == "list_files":
        payload = {"action": "list_files", "path": arguments.get("path", ".")}
    elif name == "read_file":
        payload = {"action": "read_file", "path": arguments.get("path", "")}
    elif name == "write_file":
        content = arguments.get("content", "")
        if not isinstance(content, str) or len(content) > 40000:
            return json.dumps({"ok": False, "error": "文件内容超出 40000 字符限制"}, ensure_ascii=False)
        payload = {"action": "write_file", "path": arguments.get("path", ""), "content": content}
    elif name == "run_command":
        command = arguments.get("command", "")
        workdir = arguments.get("workdir", ".")
        if not isinstance(command, str) or not command.strip() or len(command) > 3000:
            return json.dumps({"ok": False, "error": "命令为空或超过 3000 字符"}, ensure_ascii=False)
        if not isinstance(workdir, str):
            return json.dumps({"ok": False, "error": "workdir 必须是相对路径字符串"}, ensure_ascii=False)
        payload = {
            "action": "run_command",
            "command": command,
            "workdir": workdir,
            "timeout_seconds": max(1, min(120, int(remaining_seconds))),
        }
    else:
        return json.dumps({"ok": False, "error": "未知工具"}, ensure_ascii=False)
    result = await runner_call(payload)
    return json.dumps(result, ensure_ascii=False)


async def ask_model(messages: list[dict[str, Any]], timeout_seconds: float = 100) -> dict[str, Any]:
    if not CICADAS_API_KEY:
        raise RuntimeError("尚未配置 Cicadas API Key。请在 agent-ui/.env 中设置 CICADAS_API_KEY 后重启服务。")
    headers = {"Authorization": f"Bearer {CICADAS_API_KEY}", "Content-Type": "application/json"}
    body = {"model": MODEL, "messages": messages, "tools": TOOLS, "tool_choice": "auto"}
    timeout = max(1, min(100, timeout_seconds))
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(15, timeout))) as client:
        try:
            async with client.stream(
                "POST", f"{API_BASE_URL}/chat/completions", headers=headers, json=body
            ) as response:
                if response.status_code >= 400:
                    raise RuntimeError(
                        f"模型 API 返回 HTTP {response.status_code}，请检查 API 地址、密钥和模型 ID。"
                    )
                chunks: list[bytes] = []
                response_size = 0
                async for chunk in response.aiter_bytes():
                    response_size += len(chunk)
                    if response_size > MAX_MODEL_RESPONSE_BYTES:
                        raise RuntimeError("模型 API 响应超过 2 MB 限制")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"模型 API 连接失败：{type(exc).__name__}") from exc
    try:
        return json.loads(b"".join(chunks))["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("模型 API 返回格式不符合 Chat Completions 兼容格式。") from exc


def get_task(task_id: int) -> sqlite3.Row | None:
    with db_connection() as db:
        return db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def set_task(task_id: int, **values: Any) -> None:
    if not values:
        return
    columns = ", ".join(f"{key}=?" for key in values)
    with db_connection() as db:
        db.execute(f"UPDATE tasks SET {columns}, updated_at=? WHERE id=?", (*values.values(), int(time.time()), task_id))


def task_cancelled(task_id: int) -> bool:
    task = get_task(task_id)
    return task is None or bool(task["cancel_requested"])


async def process_task(task_id: int) -> None:
    task = get_task(task_id)
    if task is None or task["status"] != "running":
        return
    if task["cancel_requested"]:
        set_task(task_id, status="cancelled")
        add_event(task_id, "status", "任务已取消。")
        return
    started = time.monotonic()
    tool_call_count = 0
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task["prompt"]},
    ]
    add_event(task_id, "status", "Agent 开始处理任务。")
    try:
        for step in range(1, MAX_STEPS + 1):
            if task_cancelled(task_id):
                set_task(task_id, status="cancelled")
                add_event(task_id, "status", "任务已取消。")
                return
            if time.monotonic() - started > MAX_TASK_SECONDS:
                raise RuntimeError("达到单任务 30 分钟上限")

            remaining = MAX_TASK_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                raise RuntimeError("达到单任务 30 分钟上限")
            message = await ask_model(messages, remaining)
            messages.append(message)
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                add_event(task_id, "assistant", content.strip())

            if task_cancelled(task_id):
                set_task(task_id, status="cancelled")
                add_event(task_id, "status", "任务已取消。")
                return
            calls = message.get("tool_calls") or []
            if not calls:
                final_text = content.strip() if isinstance(content, str) else "任务结束，但模型未返回文字总结。"
                set_task(task_id, status="succeeded", output=final_text)
                add_event(task_id, "status", "任务完成。")
                return
            if len(calls) > MAX_CALLS_PER_STEP:
                raise RuntimeError(f"单轮工具调用超过 {MAX_CALLS_PER_STEP} 个限制")

            for call in calls:
                if task_cancelled(task_id):
                    set_task(task_id, status="cancelled")
                    add_event(task_id, "status", "任务已取消。")
                    return
                remaining = MAX_TASK_SECONDS - (time.monotonic() - started)
                if remaining <= 0:
                    raise RuntimeError("达到单任务 30 分钟上限")
                tool_call_count += 1
                if tool_call_count > MAX_TOOL_CALLS:
                    raise RuntimeError(f"达到最多 {MAX_TOOL_CALLS} 次工具调用限制")
                function = call.get("function") or {}
                name = function.get("name", "")
                try:
                    args = json.loads(function.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("参数必须是 JSON 对象")
                    add_event(task_id, "tool", f"工具 {name}：{json.dumps(args, ensure_ascii=False)[:3000]}")
                    result = await invoke_tool(name, args, remaining)
                except (ValueError, TypeError) as exc:
                    result = json.dumps({"ok": False, "error": f"工具参数无效：{exc}"}, ensure_ascii=False)
                add_event(task_id, "tool_result", result[:8000])
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result[:12000]})
            if step == MAX_STEPS:
                raise RuntimeError(f"达到最多 {MAX_STEPS} 轮工具调用限制")
    except Exception as exc:
        error = str(exc)[:1000]
        set_task(task_id, status="failed", error=error)
        add_event(task_id, "error", error)


async def task_worker(stop: asyncio.Event) -> None:
    while not stop.is_set():
        task_id: int | None = None
        with db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row:
                candidate_id = int(row["id"])
                cursor = db.execute(
                    "UPDATE tasks SET status='running', updated_at=? WHERE id=? AND status='queued'",
                    (int(time.time()), candidate_id),
                )
                if cursor.rowcount == 1:
                    task_id = candidate_id
        if task_id is not None:
            await process_task(task_id)
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except TimeoutError:
            pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    if len(APP_PASSWORD) < 16 or APP_PASSWORD == "change-this-to-a-long-random-password":
        raise RuntimeError("Set a unique APP_PASSWORD of at least 16 characters in agent-ui/.env")
    if len(APP_SECRET) < 32 or APP_SECRET == "change-this-to-a-different-random-secret":
        raise RuntimeError("Set a unique APP_SECRET of at least 32 characters in agent-ui/.env")
    if APP_PASSWORD == APP_SECRET:
        raise RuntimeError("APP_PASSWORD and APP_SECRET must be different")
    WORKSPACE_PATH.mkdir(parents=True, exist_ok=True)
    init_db()
    stop = asyncio.Event()
    worker = asyncio.create_task(task_worker(stop))
    yield
    stop.set()
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Agent Workbench", docs_url=None, redoc_url=None, lifespan=lifespan)
STATIC_PATH = Path(__file__).parent / "static"


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
async def index():
    return FileResponse(STATIC_PATH / "index.html", media_type="text/html; charset=utf-8")


@app.get("/static/app.js")
async def app_script():
    return FileResponse(STATIC_PATH / "app.js", media_type="text/javascript; charset=utf-8")


@app.get("/static/style.css")
async def app_styles():
    return FileResponse(STATIC_PATH / "style.css", media_type="text/css; charset=utf-8")


@app.get("/api/health")
async def health():
    return {"ok": True, "model_configured": bool(CICADAS_API_KEY), "model": MODEL}


@app.get("/api/session")
async def session_status(request: Request):
    return {"authenticated": valid_session(request.cookies.get(COOKIE_NAME))}


@app.post("/api/login")
async def login(body: LoginBody, request: Request, response: Response):
    client = request.client.host if request.client else "unknown"
    if not _login_allowed(client):
        raise HTTPException(status_code=429, detail="尝试次数过多，请一分钟后再试")
    if not APP_PASSWORD or not APP_SECRET:
        raise HTTPException(status_code=503, detail="尚未设置 APP_PASSWORD / APP_SECRET")
    if not hmac.compare_digest(body.password, APP_PASSWORD):
        raise HTTPException(status_code=401, detail="密码不正确")
    expiry = int(time.time()) + SESSION_SECONDS
    response.set_cookie(
        COOKIE_NAME,
        make_session(expiry),
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        max_age=SESSION_SECONDS,
        path="/",
    )
    return {"ok": True}


@app.post("/api/logout", dependencies=[Depends(require_auth)])
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/status", dependencies=[Depends(require_auth)])
async def status():
    return {"model": MODEL, "api_configured": bool(CICADAS_API_KEY), "max_steps": MAX_STEPS}


@app.get("/api/tasks", dependencies=[Depends(require_auth)])
async def list_tasks():
    with db_connection() as db:
        rows = db.execute("SELECT id,title,status,created_at,updated_at,error FROM tasks ORDER BY id DESC LIMIT 50").fetchall()
    return [dict(row) for row in rows]


@app.post("/api/tasks", status_code=201, dependencies=[Depends(require_auth)])
async def create_task(body: NewTaskBody):
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=422, detail="任务内容不能为空")
    now = int(time.time())
    title = prompt.splitlines()[0][:80]
    with db_connection() as db:
        queued = db.execute("SELECT COUNT(*) FROM tasks WHERE status='queued'").fetchone()[0]
        if queued >= MAX_QUEUED_TASKS:
            raise HTTPException(status_code=429, detail="任务队列已满，请稍后再提交")
        cursor = db.execute(
            "INSERT INTO tasks(title,prompt,status,created_at,updated_at) VALUES(?,?, 'queued', ?,?)",
            (title, prompt, now, now),
        )
        task_id = int(cursor.lastrowid)
        db.execute(
            "DELETE FROM tasks WHERE status IN ('succeeded','failed','cancelled') AND id NOT IN (SELECT id FROM tasks WHERE status IN ('succeeded','failed','cancelled') ORDER BY id DESC LIMIT ?)",
            (MAX_RETAINED_TASKS,),
        )
    add_event(task_id, "status", "任务已加入队列，关闭网页后仍会继续执行。")
    return {"id": task_id, "status": "queued"}


@app.get("/api/tasks/{task_id}", dependencies=[Depends(require_auth)])
async def task_detail(task_id: int):
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    with db_connection() as db:
        events = db.execute(
            "SELECT id,created_at,kind,content FROM events WHERE task_id=? ORDER BY id DESC LIMIT 200",
            (task_id,),
        ).fetchall()
    result = dict(task)
    result["events"] = [dict(event) for event in reversed(events)]
    return result


@app.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(require_auth)])
async def cancel_task(task_id: int):
    with db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        task = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task["status"] == "queued":
            db.execute("UPDATE tasks SET status='cancelled', updated_at=? WHERE id=? AND status='queued'", (int(time.time()), task_id))
            message = "排队任务已取消。"
        elif task["status"] == "running":
            db.execute("UPDATE tasks SET cancel_requested=1, updated_at=? WHERE id=? AND status='running'", (int(time.time()), task_id))
            message = "已请求取消；当前模型请求或命令结束后会停止。"
        else:
            message = "任务已结束，无需取消。"
    if message != "任务已结束，无需取消。":
        add_event(task_id, "status", message)
    return {"ok": True}
