"""
Programmatic Tool Calling (PTC) Wrapper
========================================

Wraps an underlying MCP server (stdio or http) and overlays one extra tool,
``programmatic_tool_call``, that runs Python code in a persistent subprocess
sandbox.

Inside the sandbox, user code calls underlying MCP tools via a ``tools``
object, e.g. ``tools["read_file"](path="a.txt")`` or ``tools.read_file("a.txt")``
(positional args are bound to the tool schema's parameters in declared order).
The worker emits ``tool_call`` JSON-line messages to the parent; the parent
forwards them to the inner MCP server's ``call_tool`` and replies with
``tool_result``.

Because each MCPMark agent run binds to a single MCP service, there is only
one underlying server — no ``<server>_<tool>`` prefix routing is needed.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional

from src.logger import get_logger

logger = get_logger(__name__)


# Persistent worker source. Talks JSON-line messages on stdin/stdout.
_PERSISTENT_WORKER = r'''
import os, sys, json, traceback, uuid
from io import StringIO
from contextlib import redirect_stdout, redirect_stderr

_proto_out = sys.stdout
_proto_in = sys.stdin


def _read_msg():
    line = _proto_in.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


def _write_msg(msg):
    _proto_out.write(json.dumps(msg) + "\n")
    _proto_out.flush()


def _rpc_tool_call(tool_name, args, kwargs):
    req_id = uuid.uuid4().hex
    _write_msg({"type": "tool_call", "id": req_id,
                "tool_name": tool_name,
                "args": list(args), "kwargs": kwargs})
    while True:
        msg = _read_msg()
        if msg.get("type") == "tool_result" and msg.get("id") == req_id:
            if msg.get("ok"):
                return msg.get("value")
            return f"[Tool error] {msg.get('error', 'unknown error')}"


class _ToolProxy:
    __slots__ = ("_name",)

    def __init__(self, name):
        object.__setattr__(self, "_name", name)

    def __call__(self, *args, **kwargs):
        return _rpc_tool_call(self._name, args, kwargs)


class ToolCaller:
    def __getitem__(self, key):
        return _ToolProxy(str(key))

    def __getattr__(self, name):
        return _ToolProxy(str(name))


def main():
    init = _read_msg()
    workspace = init.get("workspace") or os.getcwd()
    try:
        os.chdir(workspace)
    except Exception:
        pass

    g = {
        "__name__": "__main__",
        "tools": ToolCaller(),
        "WORKSPACE": workspace,
        "workspace_path": workspace,
    }

    _write_msg({"type": "ready"})

    while True:
        try:
            msg = _read_msg()
        except Exception as exc:
            _write_msg({"type": "done", "stdout": None, "stderr": None,
                        "error": f"Protocol error: {exc}"})
            continue

        if msg.get("type") != "exec":
            continue

        code = msg.get("code", "")
        file_path = msg.get("file_path", "<code>")
        g["__file__"] = file_path

        out_buf, err_buf, tb = StringIO(), StringIO(), None
        try:
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                exec(compile(code, file_path, "exec"), g)
        except Exception:
            tb = traceback.format_exc()

        _write_msg({"type": "done",
                    "stdout": out_buf.getvalue() or None,
                    "stderr": err_buf.getvalue() or None,
                    "error": tb})


if __name__ == "__main__":
    main()
'''


_PROGRAMMATIC_TOOL_CALL_DESCRIPTION = (
    "Execute Python code that can call env data tools via `tools[\"func_name\"](*args, **kwargs)`. "
    "State persists across calls. Use print() for output. "
    "Note: `tools[\"...\"]` only accesses env data tools listed above.\n\n"
    "USE WHEN: loops, conditionals, or chaining multiple tool calls with intermediate processing.\n"
    "Batch processing:\n"
    "```python\n"
    "results = []\n"
    "for region in ['West', 'East', 'Central']:\n"
    "    data = tools[\"query_sales\"](region)\n"
    "    total = sum(row['revenue'] for row in data)\n"
    "    results.append((region, total))\n"
    "print(max(results, key=lambda x: x[1]))\n"
    "```\n\n"
    "Conditional workflow:\n"
    "```python\n"
    "info = tools[\"get_info\"](id='A001')\n"
    "if info['status'] == 'active':\n"
    "    details = tools[\"get_details\"](id='A001')\n"
    "    print(details)\n"
    "else:\n"
    "    print('Inactive, skipped')\n"
    "```"
)


def _stringify_result(result: Any) -> Any:
    """Best-effort flatten of an MCP CallToolResult dict to JSON / text."""
    try:
        content = None
        if isinstance(result, dict):
            content = result.get("content")
        if content is None:
            content = getattr(result, "content", None)
        if content is None:
            return result

        texts: List[str] = []
        for item in content:
            text = None
            if isinstance(item, dict):
                text = item.get("text")
            else:
                text = getattr(item, "text", None)
            if text is not None:
                texts.append(text)
        if not texts:
            return result
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except (json.JSONDecodeError, TypeError):
            return joined
    except Exception:
        return str(result)


_CLAIM_DONE_DESCRIPTION = (
    "Signal that the task is fully complete. Call this tool ONLY after you have "
    "finished all required work and written every required output. Until you call "
    "it, the run will not end and you will be asked to keep working."
)


class PTCWrapper:
    """Wraps an MCP server and adds ``programmatic_tool_call`` + ``claim_done`` tools."""

    PROGRAMMATIC_TOOL_CALL = "programmatic_tool_call"
    CLAIM_DONE_TOOL = "claim_done"

    def __init__(
        self,
        inner: Any,
        workspace: Optional[str] = None,
        default_code_timeout: int = 60,
    ):
        self._inner = inner
        self._workspace = os.path.abspath(workspace) if workspace else os.getcwd()
        self._default_code_timeout = int(default_code_timeout)

        # Tool schema cache for positional → keyword arg binding.
        self._tool_param_order: Dict[str, List[str]] = {}

        # Worker process state.
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._proc_lock = asyncio.Lock()
        self._tmp_dir: Optional[str] = None
        self._script_path: Optional[str] = None

    # ------------------------------------------------------------------
    # Context manager — proxy to inner, plus our own teardown.
    # ------------------------------------------------------------------

    async def __aenter__(self):
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._kill_worker()
        if self._tmp_dir and os.path.isdir(self._tmp_dir):
            try:
                import shutil

                shutil.rmtree(self._tmp_dir, ignore_errors=True)
            except Exception:
                pass
            self._tmp_dir = None
        return await self._inner.__aexit__(exc_type, exc, tb)

    # ------------------------------------------------------------------
    # Tool surface.
    # ------------------------------------------------------------------

    async def list_tools(self) -> List[Dict[str, Any]]:
        tools = await self._inner.list_tools()

        # Cache parameter order for positional-arg binding.
        for tool in tools:
            name = tool.get("name")
            if not name:
                continue
            schema = tool.get("inputSchema") or tool.get("input_schema") or {}
            props = schema.get("properties") or {}
            # JSON object key order is preserved in dict iteration (Py3.7+).
            self._tool_param_order[name] = list(props.keys())

        tools = list(tools)
        tools.append(self._programmatic_tool_call_descriptor())
        tools.append(self._claim_done_descriptor())
        return tools

    def _programmatic_tool_call_descriptor(self) -> Dict[str, Any]:
        return {
            "name": self.PROGRAMMATIC_TOOL_CALL,
            "description": _PROGRAMMATIC_TOOL_CALL_DESCRIPTION,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "Python code. Use tools[\"func_name\"](*args, **kwargs) "
                            "to call env tools."
                        ),
                    },
                },
                "required": ["code"],
            },
        }

    def _claim_done_descriptor(self) -> Dict[str, Any]:
        return {
            "name": self.CLAIM_DONE_TOOL,
            "description": _CLAIM_DONE_DESCRIPTION,
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        }

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        if name == self.PROGRAMMATIC_TOOL_CALL:
            return await self._handle_programmatic_tool_call(arguments or {})
        if name == self.CLAIM_DONE_TOOL:
            return {
                "content": [
                    {"type": "text", "text": "You have claimed the task is done."}
                ],
                "isError": False,
            }
        return await self._inner.call_tool(name, arguments)

    # ------------------------------------------------------------------
    # programmatic_tool_call implementation.
    # ------------------------------------------------------------------

    async def _handle_programmatic_tool_call(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        code = arguments.get("code") or ""
        timeout = self._default_code_timeout

        filename = f"ptc_{uuid.uuid4().hex[:8]}.py"
        tmp_dir = self._ensure_tmp_dir()
        file_path = os.path.join(tmp_dir, filename)
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(code)
        except Exception as exc:  # noqa: BLE001
            return _ptc_text_result(f"[ptc] failed to write code file: {exc}")

        async with self._proc_lock:
            try:
                await self._ensure_worker()
            except Exception as exc:  # noqa: BLE001
                return _ptc_text_result(f"[ptc] worker failed to start: {exc}")

            try:
                await self._send({"type": "exec", "code": code, "file_path": file_path})
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                await self._kill_worker()
                return _ptc_text_result(f"[ptc] worker crashed before exec: {exc}")

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await self._kill_worker()
                    return _ptc_text_result(
                        f"[ptc] execution timed out after {timeout}s"
                    )
                try:
                    msg = await self._readline(remaining)
                except asyncio.TimeoutError:
                    await self._kill_worker()
                    return _ptc_text_result(
                        f"[ptc] execution timed out after {timeout}s"
                    )
                if msg is None:
                    await self._kill_worker()
                    return _ptc_text_result("[ptc] worker exited without output")

                mtype = msg.get("type")
                if mtype == "done":
                    return _format_exec_result(msg)
                if mtype == "tool_call":
                    await self._handle_tool_call(msg)
                    continue
                logger.warning("PTC worker sent unknown message: %s", mtype)

    async def _handle_tool_call(self, msg: Dict[str, Any]) -> None:
        req_id = msg.get("id")
        tool_name = msg.get("tool_name") or ""
        args = msg.get("args") or []
        kwargs = msg.get("kwargs") or {}

        try:
            bound_kwargs = self._bind_positional(tool_name, args, kwargs)
        except Exception as exc:  # noqa: BLE001
            await self._send({
                "type": "tool_result", "id": req_id,
                "ok": False, "error": f"argument binding failed: {exc}",
            })
            return

        if tool_name == self.PROGRAMMATIC_TOOL_CALL:
            await self._send({
                "type": "tool_result", "id": req_id,
                "ok": False,
                "error": "programmatic_tool_call cannot call itself recursively",
            })
            return

        try:
            raw = await self._inner.call_tool(tool_name, bound_kwargs)
            value = _stringify_result(raw)
            reply = {"type": "tool_result", "id": req_id, "ok": True, "value": value}
        except Exception as exc:  # noqa: BLE001
            reply = {
                "type": "tool_result", "id": req_id,
                "ok": False, "error": f"{type(exc).__name__}: {exc}",
            }

        try:
            await self._send(reply)
        except (BrokenPipeError, ConnectionResetError, OSError):
            await self._kill_worker()

    def _bind_positional(
        self, tool_name: str, args: List[Any], kwargs: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Resolve positional args against the tool's declared parameter order."""
        if not args:
            return dict(kwargs)
        order = self._tool_param_order.get(tool_name)
        if order is None:
            raise ValueError(
                f"unknown tool '{tool_name}' (positional args require a known schema)"
            )
        out = dict(kwargs)
        idx = 0
        for pname in order:
            if idx >= len(args):
                break
            if pname in out:
                continue
            out[pname] = args[idx]
            idx += 1
        if idx < len(args):
            raise ValueError(
                f"too many positional arguments for '{tool_name}': "
                f"got {len(args)}, schema declares {len(order)} parameter(s)"
            )
        return out

    # ------------------------------------------------------------------
    # Worker lifecycle.
    # ------------------------------------------------------------------

    def _ensure_tmp_dir(self) -> str:
        if self._tmp_dir and os.path.isdir(self._tmp_dir):
            return self._tmp_dir
        self._tmp_dir = tempfile.mkdtemp(prefix="mcpmark_ptc_")
        self._script_path = os.path.join(self._tmp_dir, "_ptc_worker.py")
        with open(self._script_path, "w", encoding="utf-8") as f:
            f.write(_PERSISTENT_WORKER)
        return self._tmp_dir

    async def _ensure_worker(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        if self._proc is not None:
            logger.warning(
                "PTC worker exited (code %s) — restarting (state reset)",
                self._proc.returncode,
            )
            self._proc = None

        self._ensure_tmp_dir()
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, self._script_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._workspace,
        )

        init = json.dumps({"workspace": self._workspace}) + "\n"
        self._proc.stdin.write(init.encode())
        await self._proc.stdin.drain()

        try:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=15)
            ready = json.loads(line)
            if ready.get("type") != "ready":
                raise RuntimeError(f"unexpected init response: {ready}")
        except Exception as exc:
            await self._kill_worker()
            raise RuntimeError(f"worker failed to start: {exc}") from exc

        logger.info("PTC worker started (pid %s, cwd=%s)", self._proc.pid, self._workspace)

    async def _send(self, msg: Dict[str, Any]) -> None:
        data = (json.dumps(msg) + "\n").encode()
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def _readline(self, timeout: float) -> Optional[Dict[str, Any]]:
        line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=timeout)
        if not line:
            return None
        return json.loads(line)

    async def _kill_worker(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.kill()
            await proc.wait()
        except (ProcessLookupError, OSError):
            pass


def _ptc_text_result(text: str) -> Dict[str, Any]:
    """Shape a plain string as an MCP CallToolResult-like dict."""
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _format_exec_result(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Pack a worker `done` message into an MCP CallToolResult-like dict."""
    parts: List[str] = []
    if msg.get("stdout"):
        parts.append(str(msg["stdout"]))
    if msg.get("stderr"):
        parts.append("STDERR:\n" + str(msg["stderr"]))
    if msg.get("error"):
        parts.append("ERROR:\n" + str(msg["error"]))
    text = "\n".join(parts) if parts else ""
    return {
        "content": [{"type": "text", "text": text}],
        "isError": bool(msg.get("error")),
    }
