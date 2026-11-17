from __future__ import annotations

import json
import os
import shutil
import signal
import selectors
import ctypes
import platform
import socketserver
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:
    resource = None


WORKSPACE = Path("/workspace").resolve()
SOCKET_PATH = Path("/run/agent/runner.sock")
SCRATCH_ROOT = Path("/run/scratch")
MAX_FILE_BYTES = 100_000
MAX_OUTPUT_CHARS = 20_000
MAX_COMMAND_TIMEOUT = 120
COMMAND_LOCK = threading.Lock()


class SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]


class SockFprog(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filters", ctypes.POINTER(SockFilter))]


def block_process_group_escape() -> None:
    syscall_numbers = {"x86_64": (109, 112), "amd64": (109, 112), "aarch64": (154, 157), "arm64": (154, 157)}
    numbers = syscall_numbers.get(platform.machine().lower())
    if numbers is None:
        raise RuntimeError(f"不支持的 Runner 架构：{platform.machine()}")
    setpgid_number, setsid_number = numbers
    filters = (SockFilter * 6)(
        SockFilter(0x20, 0, 0, 0),
        SockFilter(0x15, 0, 1, setpgid_number),
        SockFilter(0x06, 0, 0, 0x00050001),
        SockFilter(0x15, 0, 1, setsid_number),
        SockFilter(0x06, 0, 0, 0x00050001),
        SockFilter(0x06, 0, 0, 0x7FFF0000),
    )
    program = SockFprog(len(filters), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(38, 1, None, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
    if libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_SECCOMP failed")


def safe_path(relative: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("路径不能为空")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("只允许工作区内相对路径")
    resolved = (WORKSPACE / path).resolve(strict=False)
    if not resolved.is_relative_to(WORKSPACE):
        raise ValueError("路径不能越出工作区")
    return resolved


def validate_workspace_symlinks() -> None:
    for current, dirs, files in os.walk(WORKSPACE, followlinks=False):
        for name in dirs + files:
            path = Path(current) / name
            if path.is_symlink():
                link = Path(os.readlink(path))
                if link.is_absolute() or not path.resolve(strict=False).is_relative_to(WORKSPACE):
                    raise ValueError(f"工作区包含指向外部的符号链接：{path.relative_to(WORKSPACE)}")


def prepare_scratch(scratch: Path) -> None:
    scratch.mkdir(parents=True)
    if WORKSPACE.exists():
        validate_workspace_symlinks()
        shutil.copytree(WORKSPACE, scratch, dirs_exist_ok=True, symlinks=True)
    scratch.chmod(0o777)
    for current, dirs, files in os.walk(scratch, followlinks=False):
        current_path = Path(current)
        current_path.chmod(0o777)
        for name in dirs:
            path = current_path / name
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o007)
        for name in files:
            path = current_path / name
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o006)


def run_bounded_command(command: str, cwd: Path, timeout_seconds: int, home: Path, tmpdir: Path) -> dict[str, Any]:
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(home),
        "TMPDIR": str(tmpdir),
        "USER": "agent",
        "LOGNAME": "agent",
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "NODE_OPTIONS": "--max-old-space-size=128",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "*",
    }

    def set_limits() -> None:
        if resource is None:
            return
        resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        block_process_group_escape()

    process = subprocess.Popen(
        ["/bin/bash", "-c", command],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=True,
        user=10002,
        group=10002,
        extra_groups=[],
        preexec_fn=set_limits if resource is not None else None,
    )
    tails = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)

    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    while selector.get_map() or process.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            break
        for key, _ in selector.select(timeout=min(0.05, remaining)):
            try:
                chunk = os.read(key.fileobj.fileno(), 8192)
            except BlockingIOError:
                continue
            if not chunk:
                selector.unregister(key.fileobj)
                key.fileobj.close()
                continue
            tail = tails[key.data]
            tail.extend(chunk)
            if len(tail) > MAX_OUTPUT_CHARS:
                del tail[:-MAX_OUTPUT_CHARS]
    selector.close()
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    for stream in (process.stdout, process.stderr):
        if stream and not stream.closed:
            stream.close()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        timed_out = True
    return {
        "ok": not timed_out,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "stdout": bytes(tails["stdout"]).decode("utf-8", errors="replace"),
        "stderr": bytes(tails["stderr"]).decode("utf-8", errors="replace"),
        "note": "命令在临时工作副本中运行；命令生成的文件不会保存。请用 write_file 保存代码改动。",
    }


def run_action(request: dict[str, Any]) -> dict[str, Any]:
    action = request.get("action")
    if action == "list_files":
        root = safe_path(request.get("path", "."))
        if not root.exists() or not root.is_dir():
            raise ValueError("目录不存在")
        items: list[str] = []
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink()]
            for name in sorted(files):
                full = Path(current) / name
                items.append(full.relative_to(WORKSPACE).as_posix())
                if len(items) >= 300:
                    break
            if len(items) >= 300:
                break
        return {"ok": True, "files": items}

    if action == "read_file":
        path = safe_path(request.get("path", ""))
        if not path.is_file():
            raise ValueError("文件不存在")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("文件超过 100 KB 限制")
        return {"ok": True, "content": path.read_text(encoding="utf-8", errors="replace")}

    if action == "run_command":
        command = request.get("command", "")
        if not isinstance(command, str) or not command.strip() or len(command) > 3000:
            raise ValueError("命令为空或超过 3000 字符")
        cwd = safe_path(request.get("workdir", "."))
        if not cwd.is_dir():
            raise ValueError("工作目录不存在")
        timeout_seconds = request.get("timeout_seconds", MAX_COMMAND_TIMEOUT)
        if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= MAX_COMMAND_TIMEOUT:
            raise ValueError("命令超时必须为 1 到 120 秒")
        validate_workspace_symlinks()
        relative_cwd = cwd.relative_to(WORKSPACE)
        with COMMAND_LOCK:
            with tempfile.TemporaryDirectory(prefix="agent-task-", dir=SCRATCH_ROOT) as temp_dir:
                temp_root = Path(temp_dir)
                temp_root.chmod(0o711)
                scratch = temp_root / "workspace"
                prepare_scratch(scratch)
                home = temp_root / "home"
                tmpdir = temp_root / "tmp"
                home.mkdir(mode=0o700)
                tmpdir.mkdir(mode=0o700)
                os.chown(home, 10002, 10002)
                os.chown(tmpdir, 10002, 10002)
                scratch_cwd = (scratch / relative_cwd).resolve(strict=False)
                if not scratch_cwd.is_relative_to(scratch) or not scratch_cwd.is_dir():
                    raise ValueError("临时工作目录无效")
                return run_bounded_command(command, scratch_cwd, timeout_seconds, home, tmpdir)

    raise ValueError("未知操作")


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(100_001)
        try:
            if len(raw) > 100_000:
                raise ValueError("请求过大")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("请求必须是 JSON 对象")
            result = run_action(request)
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
        self.wfile.write(json.dumps(result, ensure_ascii=False).encode() + b"\n")


if hasattr(socketserver, "ThreadingUnixStreamServer"):
    class UnixServer(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
        allow_reuse_address = True
else:
    UnixServer = None


def main() -> None:
    if UnixServer is None:
        raise RuntimeError("当前系统不支持 Unix domain socket")
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass
    with UnixServer(str(SOCKET_PATH), Handler) as server:
        os.chmod(SOCKET_PATH, 0o660)
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
