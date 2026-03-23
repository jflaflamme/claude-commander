"""Claude Agent SDK wrapper — persistent sessions via ClaudeSDKClient."""

import asyncio
import html
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable

import anyio

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TaskProgressMessage,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import ToolPermissionContext
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import db

logger = logging.getLogger(__name__)

# Telegram message limit
TG_MAX_LEN = 4096

# Timeout for a single prompt (5 minutes)
PROMPT_TIMEOUT = 300

# Default model
DEFAULT_MODEL = "sonnet"

# Per-project persistent clients
_clients: dict[str, ClaudeSDKClient] = {}

# Per-project connection locks — prevent duplicate connect races
_connect_locks: dict[str, asyncio.Lock] = {}

# Running tasks per project (for cancel support)
_running_tasks: dict[str, asyncio.Task] = {}

# Pending permission requests: request_id -> (resolve_fn, anyio.Event)
# Uses anyio.Event for compatibility with the SDK's anyio task groups
_pending_permissions: dict[str, tuple[Callable, anyio.Event]] = {}

# Telegram bot reference (set by bot.py at startup)
_tg_bot = None
_tg_chat_id: int = 0

# Scan directories for /scan
SCAN_DIRS = [
    Path.home() / "Documents",
    Path.home() / "projects",
    Path.home() / "src",
    Path.home() / "code",
    Path.home() / "dev",
]
PROJECT_MARKERS = [
    ".git", ".mcp.json", "CLAUDE.md", "pyproject.toml",
    "package.json", "Cargo.toml", "go.mod",
]


def _interpret_prompt(prompt: str) -> str | None:
    """Return a brief task interpretation prefix, or None if unclassified."""
    p = prompt.lower()
    tags = []
    if re.search(
        r"\b(fix|bug|error|broken|fail|issue|traceback|exception)\b", p
    ):
        tags.append("Bug Fix")
    if re.search(
        r"\b(add|create|implement|build|write|make|new)\b", p
    ):
        tags.append("Feature")
    if re.search(
        r"\b(explain|what|how|why|describe|show me)\b", p
    ):
        tags.append("Question")
    if re.search(
        r"\b(refactor|clean|improve|optimize|simplify)\b", p
    ):
        tags.append("Refactor")
    if re.search(r"\b(deploy|release|ship|push)\b", p):
        tags.append("Deploy")
    if re.search(r"\b(review|check|audit|validate|verify)\b", p):
        tags.append("Review")

    if not tags:
        return None

    scope = None
    if re.search(r"\b(all|every|entire|whole|project)\b", p):
        scope = "project-wide"
    elif re.search(r"\b(api|endpoint|route)\b", p):
        scope = "API layer"
    elif re.search(r"\b(ui|frontend|page|button|form)\b", p):
        scope = "frontend"
    elif re.search(r"\b(db|database|model|migration|schema)\b", p):
        scope = "data layer"

    parts = [f"Task: {' · '.join(tags)}"]
    if scope:
        parts.append(f"Scope: {scope}")
    return f"[{' | '.join(parts)}]"


def _label_from_tool(tool: str, inp: dict) -> str:
    """Build a short status label from a tool name and its input."""
    if tool == "Bash":
        cmd = inp.get("command", "")
        if cmd:
            return f"→ Bash: {cmd[:72]}"
    elif tool in ("Write", "Edit", "Read"):
        path = inp.get("file_path", "")
        if path:
            return f"→ {tool}: {Path(path).name}"
    elif tool == "Grep":
        pattern = inp.get("pattern", "")
        if pattern:
            return f"→ Grep: {pattern[:60]}"
    elif tool == "Glob":
        pattern = inp.get("pattern", "")
        if pattern:
            return f"→ Glob: {pattern[:60]}"
    elif tool.startswith("mcp__"):
        parts = tool.split("__")
        if len(parts) >= 3:
            return f"→ {parts[1]}: {parts[2]}"
    return f"→ {tool}"


def set_telegram_bot(bot, chat_id: int) -> None:
    global _tg_bot, _tg_chat_id
    _tg_bot = bot
    _tg_chat_id = chat_id


def resolve_permission(request_id: str, allowed: bool) -> bool:
    logger.info(
        "resolve_permission: id=%s allowed=%s pending=%s",
        request_id, allowed, list(_pending_permissions.keys()),
    )
    entry = _pending_permissions.pop(request_id, None)
    if entry is not None:
        resolve_fn, event = entry
        if not event.is_set():
            resolve_fn(allowed)  # store result then set event
            logger.info("resolve_permission: event set for %s", request_id)
            return True
        logger.warning("resolve_permission: event already set for %s", request_id)
    else:
        logger.warning("resolve_permission: no entry found for %s", request_id)
    return False


def is_project_busy(project_name: str) -> bool:
    """Return True if a prompt is currently running for this project."""
    task = _running_tasks.get(project_name)
    return task is not None and not task.done()


async def cancel_running(project_name: str) -> bool:
    task = _running_tasks.get(project_name)
    logger.info(
        "[%s] cancel_running: task=%s done=%s running_tasks=%s",
        project_name,
        task,
        task.done() if task else "n/a",
        list(_running_tasks.keys()),
    )
    task = _running_tasks.pop(project_name, None)

    # Drain any queued prompts
    q = _queues.get(project_name)
    drained = 0
    if q:
        while True:
            try:
                _, future, _ = q.get_nowait()
                if not future.done():
                    future.set_result("Cancelled.")
                q.task_done()
                drained += 1
            except asyncio.QueueEmpty:
                break
    if drained:
        logger.info(
            "[%s] Drained %d queued prompt(s)", project_name, drained
        )

    if task and not task.done():
        logger.info("[%s] Cancelling task and disconnecting client", project_name)
        task.cancel()
        await reset_client(project_name)
        logger.info("[%s] Cancel complete", project_name)
        return True
    if drained:
        return True
    logger.warning("[%s] cancel_running: nothing to cancel", project_name)
    return False


def _find_mcp_config(cwd: str) -> Path | None:
    path = Path(cwd)
    for d in [path, *path.parents]:
        mcp_file = d / ".mcp.json"
        if mcp_file.exists():
            return mcp_file
        if d == Path.home():
            break
    return None


def _load_filtered_mcp(
    project_name: str, cwd: str
) -> dict[str, Any] | None:
    """Load MCP servers filtered by project permissions."""
    config_path = _find_mcp_config(cwd)
    if not config_path:
        return None

    try:
        data = json.loads(config_path.read_text())
        all_servers = data.get("mcpServers", data)
    except Exception:
        logger.warning("Failed to read %s", config_path)
        return None

    saved = db.get_project_mcp(project_name)
    if not saved:
        for name in all_servers:
            db.set_project_mcp(project_name, name, True)
        logger.info(
            "[%s] Auto-allowed MCP: %s",
            project_name, ", ".join(all_servers.keys()),
        )
        return all_servers

    allowed = db.get_allowed_mcp(project_name)
    filtered = {
        k: v for k, v in all_servers.items()
        if k in allowed
    }
    return filtered or None


def _tool_detail(tool_name: str, tool_input: dict) -> str:
    """Return a brief human-readable summary of what the tool wants to do."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return f"<code>$ {html.escape(cmd[:120])}</code>" if cmd else ""
    if tool_name in ("Write", "Edit", "Read"):
        path = tool_input.get("file_path", "")
        return f"<code>{html.escape(path[:120])}</code>" if path else ""
    if tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        return f"pattern: <code>{html.escape(pattern[:80])}</code>" if pattern else ""
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        if len(parts) >= 3:
            return f"{html.escape(parts[1])}: {html.escape(parts[2])}"
    return ""


def _make_can_use_tool(project_name: str) -> Callable:
    """Create a can_use_tool callback that sends Telegram approval buttons."""
    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        if db.is_tool_allowed(tool_name):
            return PermissionResultAllow()

        if not _tg_bot:
            return PermissionResultAllow()

        request_id = uuid.uuid4().hex[:8]

        # Use anyio.Event for compatibility with the SDK's anyio task groups.
        # asyncio.Future + asyncio.wait_for doesn't wake up correctly inside
        # anyio task group contexts (SDK uses _tg.start_soon internally).
        event = anyio.Event()
        result: list[bool] = [False]

        def _resolve(allowed: bool) -> None:
            result[0] = allowed
            event.set()

        _pending_permissions[request_id] = (_resolve, event)

        detail = _tool_detail(tool_name, tool_input)
        msg = (
            f"🔐 <b>{html.escape(project_name)}</b> wants to use "
            f"<code>{html.escape(tool_name)}</code>"
        )
        if detail:
            msg += f"\n{detail}"

        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "Allow once", callback_data=f"perm:y:{request_id}"
            ),
            InlineKeyboardButton(
                "Always", callback_data=f"perm:a:{request_id}:{tool_name}"
            ),
            InlineKeyboardButton(
                "Deny", callback_data=f"perm:n:{request_id}"
            ),
        ]])

        try:
            await _tg_bot.send_message(
                _tg_chat_id, msg,
                parse_mode="HTML",
                reply_markup=buttons,
                read_timeout=30,
                write_timeout=30,
                connect_timeout=30,
                pool_timeout=30,
            )
        except Exception as e:
            logger.warning(
                "Failed to send permission request: %s (%s)",
                e, type(e).__name__,
            )
            _pending_permissions.pop(request_id, None)
            return PermissionResultAllow()

        logger.info(
            "[%s] Waiting for permission: tool=%s id=%s",
            project_name, tool_name, request_id,
        )
        await event.wait()

        logger.info(
            "[%s] Permission resolved: tool=%s allowed=%s",
            project_name, tool_name, result[0],
        )
        if result[0]:
            return PermissionResultAllow()
        return PermissionResultDeny(
            message="User denied this tool call. Try a different approach.",
            interrupt=False,
        )

    return can_use_tool


def _build_options(
    cwd: str, project_name: str = "",
) -> ClaudeAgentOptions:
    opts = ClaudeAgentOptions(
        cwd=cwd,
        permission_mode="acceptEdits",
        setting_sources=["user", "project"],
        model=DEFAULT_MODEL,
        include_partial_messages=True,
    )
    # USE_SUBSCRIPTION=true forces OAuth even when ANTHROPIC_API_KEY is set
    # in the environment (e.g. needed by other tools in the same process).
    if os.getenv("USE_SUBSCRIPTION"):
        opts.env = {"ANTHROPIC_API_KEY": ""}
    if project_name:
        mcp = _load_filtered_mcp(project_name, cwd)
        if mcp:
            opts.mcp_servers = mcp
        opts.can_use_tool = _make_can_use_tool(project_name)
    else:
        mcp_path = _find_mcp_config(cwd)
        if mcp_path:
            opts.mcp_servers = mcp_path
    return opts


async def _get_client(
    project_name: str, cwd: str
) -> ClaudeSDKClient:
    """Get or create a persistent client for a project."""
    if project_name in _clients:
        return _clients[project_name]

    if project_name not in _connect_locks:
        _connect_locks[project_name] = asyncio.Lock()

    async with _connect_locks[project_name]:
        # Re-check after acquiring lock — another caller may have connected
        if project_name in _clients:
            return _clients[project_name]

        logger.info("[%s] Connecting...", project_name)
        opts = _build_options(cwd, project_name)
        client = ClaudeSDKClient(options=opts)
        await client.connect()
        _clients[project_name] = client

        if isinstance(opts.mcp_servers, dict) and opts.mcp_servers:
            mcp_names = ", ".join(opts.mcp_servers.keys())
            logger.info(
                "[%s] Loaded — MCP: %s", project_name, mcp_names
            )
        else:
            logger.info("[%s] Loaded — no MCP", project_name)

    return _clients[project_name]


async def disconnect_client(project_name: str) -> None:
    """Disconnect and remove a project's client."""
    client = _clients.pop(project_name, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
        logger.info("[%s] Client disconnected", project_name)


async def reset_client(project_name: str) -> None:
    """Reset: disconnect and clear session."""
    await disconnect_client(project_name)
    db.reset_session(project_name)


async def warmup_projects() -> None:
    """Pre-connect clients for all registered projects."""
    projects = db.list_projects()
    for p in projects:
        name = p["name"]
        cwd = p["cwd"]
        try:
            await _get_client(name, cwd)
        except Exception as e:
            logger.warning(
                "[%s] Warmup failed: %s", name, e
            )


async def _handle_message(
    message: Any,
    project_name: str,
    emit: Callable[[str], Any],
) -> None:
    """Dispatch SDK messages to status updates and session tracking."""
    # TaskProgressMessage is a SystemMessage subclass — check first
    if isinstance(message, TaskProgressMessage):
        label = message.description or (
            _label_from_tool(message.last_tool_name, {})
            if message.last_tool_name else None
        )
        if label:
            logger.debug("[%s] Progress: %s", project_name, label)
            await emit(label)

    elif isinstance(message, SystemMessage):
        if message.subtype == "init":
            sid = message.data.get("session_id")
            if sid:
                db.save_session(project_name, sid)

    elif isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                label = _label_from_tool(block.name, block.input)
                logger.debug("[%s] Tool: %s", project_name, label)
                await emit(label)
            elif isinstance(block, TextBlock):
                text = block.text.strip()
                if text:
                    first_line = text.split("\n")[0][:80]
                    logger.debug(
                        "[%s] Text: %s", project_name, first_line
                    )
                    await emit(f"→ {first_line}")


async def run_prompt(
    project_name: str,
    prompt: str,
    on_status: Callable[[str], Any] | None = None,
) -> str:
    """Send a prompt to a persistent client."""
    project = db.get_project(project_name)
    if not project:
        return f"Project '{project_name}' not found."

    cwd = project["cwd"]

    try:
        client = await _get_client(project_name, cwd)
    except Exception as e:
        logger.exception(
            "[%s] Failed to connect", project_name
        )
        return f"Connection error: {e}"

    preview = prompt[:80] + ("..." if len(prompt) > 80 else "")
    logger.info("[%s] Prompt: %s", project_name, preview)

    interp = _interpret_prompt(prompt)
    if interp:
        logger.debug("[%s] Interpretation: %s", project_name, interp)
        prompt = f"{interp}\n{prompt}"

    async def _emit(label: str) -> None:
        if on_status:
            try:
                await on_status(label)
            except Exception:
                pass

    try:
        await client.query(prompt)

        result_text = ""
        async for message in client.receive_response():
            await _handle_message(
                message, project_name, _emit
            )
            if isinstance(message, ResultMessage):
                result_text = message.result or "(no output)"
                logger.info(
                    "[%s] Result (%d chars)",
                    project_name, len(result_text),
                )

        db.touch_session(project_name)
        return result_text

    except asyncio.CancelledError:
        logger.info("[%s] Cancelled", project_name)
        return "Cancelled."
    except Exception as e:
        logger.warning(
            "[%s] Error: %s — reconnecting", project_name, e
        )
        await disconnect_client(project_name)
        try:
            client = await _get_client(project_name, cwd)
            await client.query(prompt)
            result_text = ""
            async for message in client.receive_response():
                await _handle_message(
                    message, project_name, _emit
                )
                if isinstance(message, ResultMessage):
                    result_text = (
                        message.result or "(no output)"
                    )
            db.touch_session(project_name)
            return result_text
        except Exception as retry_err:
            logger.exception(
                "[%s] Retry failed", project_name
            )
            return f"Error: {retry_err}"


# Per-project prompt queues
_queues: dict[str, asyncio.Queue] = {}


def _get_queue(project_name: str) -> asyncio.Queue:
    if project_name not in _queues:
        _queues[project_name] = asyncio.Queue()
    return _queues[project_name]


async def run_prompt_queued(
    project_name: str,
    prompt: str,
    on_status: Callable[[str], Any] | None = None,
) -> str:
    """Queue-wrapped run_prompt with cancel support."""
    q = _get_queue(project_name)
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    await q.put((prompt, future, on_status))

    if not getattr(q, "_worker_running", False):
        q._worker_running = True
        asyncio.create_task(_queue_worker(project_name, q))

    return await future


async def _queue_worker(
    project_name: str, q: asyncio.Queue
) -> None:
    try:
        while True:
            try:
                prompt, future, on_status = q.get_nowait()
            except asyncio.QueueEmpty:
                break
            task = asyncio.create_task(
                run_prompt(project_name, prompt, on_status)
            )
            _running_tasks[project_name] = task
            logger.info("[%s] Task registered: %s", project_name, task)
            try:
                result = await task
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                logger.info("[%s] Task was cancelled", project_name)
                if not future.done():
                    future.set_result("Cancelled.")
            except Exception as e:
                if not future.done():
                    future.set_exception(e)
            finally:
                _running_tasks.pop(project_name, None)
                q.task_done()
    finally:
        q._worker_running = False


def reset_memory(project_name: str) -> None:
    """Clear session — schedules client disconnect."""
    asyncio.get_event_loop().create_task(
        reset_client(project_name)
    )


# --- Project scanning ---


def get_mcp_servers_for_project(cwd: str) -> list[str]:
    config_path = _find_mcp_config(cwd)
    if not config_path:
        return []
    try:
        data = json.loads(config_path.read_text())
        servers = data.get("mcpServers", data)
        return list(servers.keys())
    except Exception:
        return []


def scan_projects() -> list[dict]:
    found = []
    for scan_dir in SCAN_DIRS:
        if not scan_dir.exists():
            continue
        for child in sorted(scan_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            markers = [
                m for m in PROJECT_MARKERS
                if (child / m).exists()
            ]
            if markers:
                desc = _auto_description(child)
                found.append({
                    "path": str(child),
                    "name": child.name,
                    "markers": markers,
                    "description": desc,
                })
    return found


def _auto_description(path: Path) -> str:
    pp = path / "pyproject.toml"
    if pp.exists():
        try:
            text = pp.read_text()
            m = re.search(
                r'description\s*=\s*"([^"]+)"', text
            )
            if m:
                return m.group(1)[:100]
        except Exception:
            pass
    pj = path / "package.json"
    if pj.exists():
        try:
            data = json.loads(pj.read_text())
            if data.get("description"):
                return data["description"][:100]
        except Exception:
            pass
    cm = path / "CLAUDE.md"
    if cm.exists():
        try:
            for line in cm.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    return line[:100]
        except Exception:
            pass
    return ""


def match_project_by_description(
    prompt: str, projects: list[dict]
) -> str | None:
    prompt_lower = prompt.lower()
    best_name = None
    best_score = 0
    for p in projects:
        desc = (p.get("description") or "").lower()
        name = p["name"].lower()
        score = 0
        if name in prompt_lower:
            score += 10
        for word in desc.split():
            if len(word) > 3 and word in prompt_lower:
                score += 2
        for word in prompt_lower.split():
            if len(word) > 3 and word in desc:
                score += 1
        if score > best_score:
            best_score = score
            best_name = p["name"]
    if best_score >= 3:
        return best_name
    return None


# --- Formatting ---


def format_html(text: str) -> str:
    code_blocks = []

    def _save_block(m):
        code_blocks.append(m.group(1))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    text = re.sub(
        r"```\w*\n(.*?)```", _save_block,
        text, flags=re.DOTALL,
    )

    # Extract markdown tables — Telegram has no table support, wrap as code
    def _save_table(m):
        code_blocks.append(m.group(0).rstrip())
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    text = re.sub(
        r"((?:^[ \t]*\|.+\n){2,})",
        _save_table,
        text,
        flags=re.MULTILINE,
    )

    inline_codes = []

    def _save_inline(m):
        inline_codes.append(m.group(1))
        return f"\x00INLINE{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", _save_inline, text)
    text = html.escape(text)
    for i, block in enumerate(code_blocks):
        text = text.replace(
            f"\x00CODEBLOCK{i}\x00",
            f"<pre>{html.escape(block)}</pre>",
        )
    for i, code in enumerate(inline_codes):
        text = text.replace(
            f"\x00INLINE{i}\x00",
            f"<code>{html.escape(code)}</code>",
        )
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(
        r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text
    )
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r'<a href="\2">\1</a>', text,
    )
    text = re.sub(
        r"((?:^&gt; .+\n?)+)",
        lambda m: "<blockquote>"
        + re.sub(
            r"^&gt; ", "", m.group(0),
            flags=re.MULTILINE
        )
        + "</blockquote>",
        text, flags=re.MULTILINE,
    )
    text = re.sub(
        r"^#{1,6}\s+(.+)$", r"<b>\1</b>",
        text, flags=re.MULTILINE,
    )
    text = re.sub(
        r"^[-*]{3,}\s*$", "---",
        text, flags=re.MULTILINE,
    )
    return text


def strip_markdown(text: str) -> str:
    text = re.sub(r"```\w*\n?", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*{1,2}(.+?)\*{1,2}", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text
    )
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(
        r"^#{1,6}\s+", "", text, flags=re.MULTILINE
    )
    return text


def split_message(
    text: str, max_len: int = TG_MAX_LEN
) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks
