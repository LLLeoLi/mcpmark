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

import ast
import asyncio
import datetime as _datetime
import difflib
import json
import os
import re
import sys
import tempfile
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

from src.logger import get_logger

logger = get_logger(__name__)


# Persistent worker source. Talks JSON-line messages on stdin/stdout.
_PERSISTENT_WORKER = r'''
import os, sys, json, csv, traceback, uuid
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
            # Raise (rather than return an error string) so failures surface
            # as exceptions, matching the training-time PTC sandbox where
            # env.tools[name] raises — try/except around tool calls works and
            # errors never flow onward disguised as data.
            raise RuntimeError(msg.get("error", "unknown error"))


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
        # Pre-imports, matching the training-time PTC sandbox init.
        "os": os,
        "sys": sys,
        "json": json,
        "csv": csv,
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


# Kept in sync with task-sync `eval.py` / verl `tasksync_ptc_agent_loop.py`
# (PTC_TOOL_DESCRIPTION_RICH) so deployment matches what the model saw in
# training. Update all three together.
_PROGRAMMATIC_TOOL_CALL_DESCRIPTION = (
    'Run Python that calls the tools listed above as `tools["tool_name"](*args, **kwargs)`. State (variables, imports) persists across calls; use print() to see output.\n\n'
    "USE WHEN: loops, conditionals, error handling, or chaining multiple tool calls with intermediate processing.\n\n"
    "Notes:\n"
    "- Code runs in the workspace directory and file writes are restricted to it; os, json, csv, sys are pre-imported.\n"
    "- Tools return native Python values; the type and structure vary by tool (e.g. dict, list, or str), so a quick `print(type(r), repr(r)[:200])` on one result shows the shape before processing many.\n"
    "- Very large printed output is truncated; print summaries rather than large raw data.\n"
    "- On an exception the traceback is returned; variables and tool side effects from lines that already ran are kept.\n"
    "- Each call has an execution time limit; long loops can be split across calls.\n\n"
    "Usage examples:\n\n"
    "Batch processing:\n"
    "```python\n"
    "results = []\n"
    "for region in ['West', 'East', 'Central']:\n"
    "    data = tools[\"query_sales\"](region=region)\n"
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
    "```\n\n"
    "Error handling (tools may raise or return error payloads):\n"
    "```python\n"
    "ok, failed = [], []\n"
    "for item_id in ['A001', 'A002', 'A003']:\n"
    "    try:\n"
    "        ok.append(tools[\"get_info\"](id=item_id))\n"
    "    except Exception as e:\n"
    "        failed.append((item_id, str(e)))\n"
    "print(f'{len(ok)} ok, {len(failed)} failed:', failed[:3])\n"
    "```"
)


# Whitelisted names for reconstructing Python `repr` payloads (e.g. postgres-mcp
# returns ``str(list[dict])`` where cells may be Decimal/datetime/UUID). No
# ``__builtins__`` — a malicious value like ``[__import__('os').system(...)]``
# raises NameError and falls back to the raw string rather than executing.
_REPR_EVAL_NS: Dict[str, Any] = {
    "__builtins__": {},
    "Decimal": Decimal,
    "datetime": _datetime,
    "date": _datetime.date,
    "time": _datetime.time,
    "timedelta": _datetime.timedelta,
    "UUID": UUID,
}


def _coerce_block(text: str) -> tuple:
    """Recover a Python value from one text block; ``(value, ok)``.

    Structured payloads become native objects, plain text stays ``str``:

      1. strict JSON (canonical structured channel for well-behaved servers);
      2. a Python ``repr`` container — only attempted when the text looks like a
         top-level ``[``/``{``/``(`` collection, so genuine prose (file contents,
         error strings) is never mis-parsed. ``ast.literal_eval`` handles pure
         literals; a whitelisted ``eval`` recovers ``Decimal``/``datetime``/``UUID``;
      3. otherwise ``(text, False)`` — caller keeps it as a string.
    """
    try:
        return json.loads(text), True
    except (json.JSONDecodeError, TypeError):
        pass
    if text.lstrip()[:1] not in ("[", "{", "("):
        return text, False
    try:
        return ast.literal_eval(text), True
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        pass
    try:
        return eval(text, _REPR_EVAL_NS), True  # noqa: S307 — builtins disabled
    except Exception:  # noqa: BLE001 — any failure => keep as string
        return text, False


def _is_text_envelope(structured: Any) -> bool:
    """True when ``structuredContent`` is merely the SDK's auto-wrapper around
    text content blocks, not real structured data.

    Servers whose tools return unstructured text (e.g. postgres-mcp, which emits
    the rows as ``str(list[dict])``) get an auto-generated
    ``{"result": [{"type": "text", "text": "<repr>"}]}`` envelope that just
    re-boxes the same payload already in ``content``. Returning that envelope
    hands the model a useless nested shell instead of the rows, so we detect it
    and fall through to parsing the text into a real native structure.
    """
    if not isinstance(structured, dict) or set(structured) != {"result"}:
        return False
    inner = structured["result"]
    return isinstance(inner, list) and all(
        isinstance(b, dict) and b.get("type") == "text" for b in inner
    )


def _stringify_result(result: Any) -> Any:
    """Recover a *native* Python value from an MCP ``CallToolResult``.

    Training-time PTC (verl ``tasksync_agent_loop``) calls ``env.tools[name]``
    in-process and returns its native Python object, so trained models expect
    ``tools[...]`` to yield ``dict``/``list``/scalar. Here the value has crossed
    the MCP boundary as text content blocks, so we always parse it back into a
    native structure and do so *deterministically* — the same tool yields the
    same type on every call. That predictability matters: a value that is
    "sometimes a parsed list, sometimes its raw string" makes generated code
    guess wrong (``json.loads`` on an already-parsed list, or ``row['x']`` on an
    unparsed string), which is exactly the failure loop native returns avoid.

      1. use ``structuredContent`` when it is genuine native data — but skip the
         SDK's text-block envelope (unwrapped and parsed via steps 2/3 instead);
      2. coerce the joined text content *once* — JSON, then a Python ``repr``
         container (recovering ``Decimal``/``datetime``/``UUID``);
      3. only genuinely unparseable prose (file contents, error strings) stays
         ``str`` — itself consistent, since such a tool always returns prose.
    """
    try:
        if isinstance(result, dict):
            structured = result.get("structuredContent")
            content = result.get("content")
        else:
            structured = getattr(result, "structuredContent", None)
            content = getattr(result, "content", None)
        if isinstance(structured, (dict, list)) and not _is_text_envelope(structured):
            return structured

        if content is None:
            return result

        texts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
            else:
                text = getattr(item, "text", None)
            if text is not None:
                texts.append(text)
        if not texts:
            return result

        # Coerce the joined payload exactly once, so a given tool's result — and
        # therefore its type — is deterministic across calls.
        value, _ = _coerce_block("\n".join(texts))
        return value
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
        # Tool descriptions, used to suggest candidates for unknown tool names.
        self._tool_descriptions: Dict[str, str] = {}

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
            self._tool_descriptions[name] = tool.get("description") or ""

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
        if self._tool_param_order and name not in self._tool_param_order:
            return _ptc_text_result(self._unknown_tool_message(name))
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
            timeout_msg = (
                f"[ptc] execution timed out after {timeout}s; the Python "
                "session was restarted and its variables were lost"
            )
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await self._kill_worker()
                    return _ptc_text_result(timeout_msg)
                try:
                    msg = await self._readline(remaining)
                except asyncio.TimeoutError:
                    await self._kill_worker()
                    return _ptc_text_result(timeout_msg)
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

        if tool_name in (self.PROGRAMMATIC_TOOL_CALL, self.CLAIM_DONE_TOOL):
            # Neither recursion nor claim_done belongs inside the sandbox;
            # previously claim_done fell through to the inner server, which
            # replied with an opaque "Method not found".
            await self._send({
                "type": "tool_result", "id": req_id,
                "ok": False,
                "error": (
                    f"'{tool_name}' must be invoked as a direct tool call, "
                    "not from inside programmatic_tool_call"
                ),
            })
            return

        # Self-correction for hallucinated tool names: surface valid candidates
        # instead of letting the inner server reply with an opaque
        # "Method <name> not found".
        if self._tool_param_order and tool_name not in self._tool_param_order:
            await self._send({
                "type": "tool_result", "id": req_id,
                "ok": False, "error": self._unknown_tool_message(tool_name),
            })
            return

        try:
            bound_kwargs = self._bind_positional(tool_name, args, kwargs)
        except Exception as exc:  # noqa: BLE001
            await self._send({
                "type": "tool_result", "id": req_id,
                "ok": False, "error": f"argument binding failed: {exc}",
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
    # Self-correction for unknown tool names.
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenize(text: str) -> set:
        return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t}

    def _closest_tools(self, tool_name: str, n: int = 3) -> List[str]:
        """Rank known tool names by similarity to a (probably misremembered) name.

        Tool names follow a REST-verb convention (``API-post-page`` to *create* a
        page), so a name-only fuzzy match misleads — the model's intent ("create
        a page") lives in the description. Score each candidate on token overlap
        against name + description, tie-broken by raw name similarity, so e.g.
        ``API-create-a-page`` surfaces ``API-post-page`` ("Notion | Create a page").
        """
        wanted = self._tokenize(tool_name)
        scored = []
        for name in self._tool_param_order:
            if name in (self.PROGRAMMATIC_TOOL_CALL, self.CLAIM_DONE_TOOL):
                continue
            tokens = self._tokenize(name) | self._tokenize(
                self._tool_descriptions.get(name, "")
            )
            overlap = len(wanted & tokens) / len(wanted) if wanted else 0.0
            name_ratio = difflib.SequenceMatcher(None, tool_name, name).ratio()
            score = overlap + 0.3 * name_ratio
            if score > 0:
                scored.append((score, name))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [name for _, name in scored[:n]]

    def _unknown_tool_message(self, tool_name: str) -> str:
        suggestions = self._closest_tools(tool_name)
        if suggestions:
            hint = "Did you mean: " + ", ".join(suggestions) + "?"
        else:
            hint = "See the available tools list for valid names."
        return (
            f"Unknown tool '{tool_name}'. {hint} "
            "Tool names must match the listed names exactly."
        )

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


# Cap on the output a single programmatic_tool_call may return (chars). Agents
# occasionally print entire fetched datasets (hundreds of KB), which poisons
# the context. 0 disables. Mirrors the training-side sandbox cap
# (verl landlock_sandbox / task-sync python_sandbox).
_MAX_OUTPUT_CHARS = int(os.getenv("MCPMARK_PTC_MAX_OUTPUT_CHARS", "10000"))


def _truncate_output(text: str, limit: int) -> str:
    """Middle-truncate `text` to ~`limit` chars, keeping head and tail."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    omitted = len(text) - head - tail
    return (
        f"{text[:head]}\n...[output truncated: {omitted} chars omitted; "
        f"print concise summaries instead of large raw data]...\n{text[-tail:]}"
    )


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
    text = _truncate_output("\n".join(parts) if parts else "", _MAX_OUTPUT_CHARS)
    return {
        "content": [{"type": "text", "text": text}],
        "isError": bool(msg.get("error")),
    }
