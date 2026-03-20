"""Claude Agent SDK wrapper — persistent sessions via ClaudeSDKClient."""

import asyncio
import html
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
)

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

# Pending permission requests: request_id -> Future[bool]
_pending_permissions: dict[str, asyncio.Future] = {}

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


def _message_label(message: Any) -> str | None:
    """Extract a short label from an intermediate SDK message."""
    tool = (
        getattr(message, "tool_name", None)
        or getattr(message, "name", None)
    )
    if not tool:
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return f"→ {content.strip()[:60]}"
        return None

    inp = getattr(message, "tool_input", None) or {}

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
        # mcp__joplin__get_note → joplin: get_note
        parts = tool.split("__")
        if len(parts) >= 3:
            return f"→ {parts[1]}: {parts[2]}"

    return f"→ {tool}"


def set_telegram_bot(bot, chat_id: int) -> None:
    global _tg_bot, _tg_chat_id
    _tg_bot = bot
    _tg_chat_id = chat_id


def resolve_permission(request_id: str, allowed: bool) -> bool:
    future = _pending_permissions.pop(request_id, None)
    if future and not future.done():
        future.set_result(allowed)
        return True
    return False


def cancel_running(project_name: str) -> bool:
    task = _running_tasks.pop(project_name, None)
    if task and not task.done():
        task.cancel()
        # Disconnect the client to kill the subprocess immediately
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(reset_client(project_name))
        except RuntimeError:
            pass
        return True
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


def _build_options(
    cwd: str, project_name: str = "",
) -> ClaudeAgentOptions:
    opts = ClaudeAgentOptions(
        cwd=cwd,
        permission_mode="bypassPermissions",
        setting_sources=["user", "project"],
        model=DEFAULT_MODEL,
    )
    if project_name:
        mcp = _load_filtered_mcp(project_name, cwd)
        if mcp:
            opts.mcp_servers = mcp
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
            if isinstance(message, SystemMessage):
                if message.subtype == "init":
                    sid = message.data.get("session_id")
                    if sid:
                        db.save_session(project_name, sid)
            elif isinstance(message, ResultMessage):
                result_text = message.result or "(no output)"
                logger.info(
                    "[%s] Result (%d chars)",
                    project_name, len(result_text),
                )
            else:
                label = _message_label(message)
                if label:
                    logger.debug(
                        "[%s] Event: %s", project_name, label
                    )
                    await _emit(label)

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
                if isinstance(message, ResultMessage):
                    result_text = (
                        message.result or "(no output)"
                    )
                else:
                    label = _message_label(message)
                    if label:
                        await _emit(label)
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
            try:
                result = await task
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
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
