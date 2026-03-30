"""Claude Commander — Telegram bot for managing Claude Code sessions."""

import asyncio
import difflib
import html
import io
import logging
import os
import re
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
import time

from claude_runner import (
    IDLE_DISCONNECT,
    INACTIVITY_TIMEOUT,
    PROMPT_TIMEOUT,
    cancel_running,
    disconnect_client,
    format_html,
    get_last_activity,
    get_mcp_servers_for_project,
    idle_reaper,
    is_awaiting_permission,
    is_project_busy,
    reset_memory,
    match_project_by_description,
    resolve_permission,
    run_prompt_queued,
    scan_projects,
    set_telegram_bot,
    split_message,
    strip_markdown,
    _clients,
    _running_tasks,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("claude_runner").setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ASYNC_FEEDBACK = os.getenv("ASYNC_FEEDBACK", "").lower() in ("1", "true", "yes")
DROP_PENDING = os.getenv(
    "DROP_PENDING_UPDATES", "true"
).lower() not in ("0", "false", "no")

# Heartbeat toggle (on by default, controllable via /heartbeat)
_heartbeat_enabled: bool = True

try:
    from groq import AsyncGroq as _AsyncGroq
    _groq_client = _AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
except ImportError:
    _groq_client = None

_active_project: dict[int, str] = {}

# Last prompt per project — for retry after timeout
_last_prompt: dict[str, str] = {}


def is_admin(update: Update) -> bool:
    # Reject edited messages (update.message is None)
    if not update.message:
        return False
    return (
        update.effective_user is not None
        and update.effective_user.id == ADMIN_CHAT_ID
    )


def _get_active_project(user_id: int) -> str | None:
    if user_id in _active_project:
        name = _active_project[user_id]
        if name == COMMANDER_PROJECT or db.get_project(name):
            return name
        del _active_project[user_id]
    return None


async def _send_result(
    update: Update, result: str, project_name: str = "",
    suggested_actions: list[str] | None = None,
) -> None:
    """Send result with optional quick-reply buttons from suggested actions."""
    formatted = format_html(result)
    chunks = split_message(formatted)

    # Send all chunks except last without buttons
    for chunk in chunks[:-1]:
        try:
            await update.message.reply_text(
                chunk, parse_mode="HTML"
            )
        except Exception:
            plain = strip_markdown(chunk)
            await update.message.reply_text(plain)

    # Last chunk gets quick-reply buttons
    last = chunks[-1] if chunks else "(no output)"
    buttons = _build_quick_replies(
        suggested_actions, project_name
    )

    try:
        await update.message.reply_text(
            last, parse_mode="HTML",
            reply_markup=buttons,
        )
    except Exception as e:
        logger.warning("HTML rejected: %s", e)
        plain = strip_markdown(
            chunks[-1] if chunks else result
        )
        await update.message.reply_text(
            plain, reply_markup=buttons
        )


def _build_quick_replies(
    suggested_actions: list[str] | None, project_name: str
) -> InlineKeyboardMarkup | None:
    """Build quick-reply buttons from suggested actions."""
    if not project_name or not suggested_actions:
        return None

    buttons = [
        InlineKeyboardButton(
            action,
            callback_data=f"qr:{project_name}:{action}",
        )
        for action in suggested_actions[:3]  # Max 3 buttons
    ]

    if not buttons:
        return None

    rows = [
        buttons[i:i + 2] for i in range(0, len(buttons), 2)
    ]
    return InlineKeyboardMarkup(rows)


_FILE_EXTENSIONS = frozenset([
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".csv", ".xlsx", ".docx", ".pptx", ".ppt",
    ".txt", ".zip", ".svg", ".mp4", ".json", ".html",
    ".py", ".js", ".ts", ".xml", ".yaml", ".yml",
    ".md", ".log", ".tar", ".gz",
])
_FILE_PATH_RE = re.compile(
    r"(?<!\w)((?:/tmp|/home|/root|/var|/data|/opt)"
    r"[\w./\-_ ]+\.(?:pdf|png|jpe?g|gif|webp|csv|xlsx"
    r"|docx|pptx?|txt|zip|svg|mp4|json|html"
    r"|py|js|ts|xml|ya?ml|md|log|tar|gz))",
    re.IGNORECASE,
)


async def _send_files_from_result(
    update: Update, result: str
) -> None:
    """Detect file paths in result and send as Telegram attachments."""
    paths = _FILE_PATH_RE.findall(result)
    seen: set[str] = set()
    for path in paths:
        path = path.strip()
        if path in seen:
            continue
        seen.add(path)
        p = Path(path)
        if not p.exists() or not p.is_file():
            continue
        ext = p.suffix.lower()
        try:
            with open(p, "rb") as f:
                if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                    await update.message.reply_photo(photo=f)
                else:
                    await update.message.reply_document(document=f)
        except Exception as e:
            logger.warning("Failed to send file %s: %s", path, e)



# --- Command registry ---

COMMANDS: list[tuple[str, ..., str]] = []


def cmd(name: str, usage: str):
    def decorator(func):
        COMMANDS.append((name, func, usage))
        return func
    return decorator


@cmd("help", "show this help")
async def cmd_help(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_admin(update):
        await update.message.reply_text("Unauthorized.")
        return
    lines = ["Claude Commander\n\nCommands:"]
    for name, _, usage in COMMANDS:
        lines.append(f"/{name} — {usage}")
    await update.message.reply_text("\n".join(lines))


@cmd("projects", "list registered projects")
async def cmd_projects(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_admin(update):
        return
    projects = db.list_projects()
    if not projects:
        await update.message.reply_text(
            "No projects registered. Use /add or /scan"
        )
        return

    user_id = update.effective_user.id
    switched = _active_project.get(user_id)
    lines = []
    for p in projects:
        status = (
            "active" if p.get("session_id") else "idle"
        )
        desc = (
            f" — {p['description']}"
            if p.get("description") else ""
        )
        cur = " *" if p["name"] == switched else ""
        lines.append(
            f"• {p['name']} [{status}]{desc}{cur}"
            f"\n  {p['cwd']}"
        )

    buttons = [
        InlineKeyboardButton(
            p["name"],
            callback_data=f"switch:{p['name']}",
        )
        for p in projects
    ]

    await update.message.reply_text(
        "\n\n".join(lines)
        + "\n\n(* = default)\nTap to switch:",
        reply_markup=InlineKeyboardMarkup([buttons]),
    )


@cmd("add", "<name> <path> [desc] — register a project")
async def cmd_add(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_admin(update):
        return
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Usage: /add <name> <path> [description]"
        )
        return
    name = args[0]
    cwd = os.path.expanduser(args[1])
    desc = " ".join(args[2:]) if len(args) > 2 else ""
    if not os.path.isdir(cwd):
        await update.message.reply_text(
            f"Directory not found: {cwd}"
        )
        return
    db.add_project(name, cwd, desc)
    await update.message.reply_text(
        f"Project '{name}' registered at {cwd}"
    )

    # Show MCP setup if .mcp.json found
    servers = get_mcp_servers_for_project(cwd)
    if servers:
        buttons = []
        for s in servers:
            buttons.append([
                InlineKeyboardButton(
                    f"Allow {s}",
                    callback_data=f"mcp:y:{name}:{s}",
                ),
                InlineKeyboardButton(
                    f"Deny {s}",
                    callback_data=f"mcp:n:{name}:{s}",
                ),
            ])
        buttons.append([
            InlineKeyboardButton(
                "Allow all",
                callback_data=f"mcp:all:{name}",
            ),
        ])
        await update.message.reply_text(
            f"Found {len(servers)} MCP servers. "
            "Choose which to enable:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


@cmd("scan", "find projects and add via buttons")
async def cmd_scan(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Scan directories for projects, show as buttons."""
    if not is_admin(update):
        return

    await update.message.reply_text("Scanning...")
    found = scan_projects()

    if not found:
        await update.message.reply_text(
            "No projects found in scan directories."
        )
        return

    # Filter out already registered
    existing = {
        p["cwd"] for p in db.list_projects()
    }
    new = [
        p for p in found if p["path"] not in existing
    ]

    if not new:
        await update.message.reply_text(
            f"Found {len(found)} projects, "
            "all already registered."
        )
        return

    # Show as inline buttons (max 20)
    buttons = []
    for p in new[:20]:
        markers = ", ".join(p["markers"][:3])
        label = f"{p['name']} ({markers})"
        # Callback data max 64 bytes — use index
        buttons.append([
            InlineKeyboardButton(
                label,
                callback_data=f"scan:{p['name']}",
            )
        ])

    # "Add all" button when multiple projects found
    if len(new) > 1:
        buttons.append([
            InlineKeyboardButton(
                f"Add all {len(new)} projects",
                callback_data="scan:__all__",
            )
        ])

    # Store scan results for callback
    context.bot_data["scan_results"] = {
        p["name"]: p for p in new
    }

    await update.message.reply_text(
        f"Found {len(new)} new projects. Tap to add:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@cmd("remove", "<name> — unregister a project")
async def cmd_remove(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /remove <name>"
        )
        return
    name = context.args[0]
    if db.remove_project(name):
        for uid, pname in list(_active_project.items()):
            if pname == name:
                del _active_project[uid]
        await update.message.reply_text(
            f"Project '{name}' removed."
        )
    else:
        await update.message.reply_text(
            f"Project '{name}' not found."
        )


@cmd("ask", "<project> <prompt> — send prompt")
async def cmd_ask(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_admin(update):
        return
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Usage: /ask <project> <prompt>"
        )
        return

    project_name = args[0]
    prompt = " ".join(args[1:])
    project = db.get_project(project_name)
    if not project:
        await update.message.reply_text(
            f"Unknown project: {project_name}"
        )
        return

    _active_project[update.effective_user.id] = project_name
    await _run_and_reply(update, project_name, prompt)


@cmd("switch", "[project] — set default for plain text")
async def cmd_switch(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_admin(update):
        return
    if not context.args:
        user_id = update.effective_user.id
        current = _active_project.get(user_id, "none")
        projects = db.list_projects()
        if not projects:
            await update.message.reply_text(
                "No projects registered."
            )
            return
        buttons = [
            InlineKeyboardButton(
                p["name"],
                callback_data=f"switch:{p['name']}",
            )
            for p in projects
        ]
        # Add commander meta-project
        buttons.append(InlineKeyboardButton(
            "commander",
            callback_data=f"switch:{COMMANDER_PROJECT}",
        ))
        await update.message.reply_text(
            f"Current: {current}\nTap to switch:",
            reply_markup=InlineKeyboardMarkup([buttons]),
        )
        return

    name = context.args[0]
    if name in (COMMANDER_PROJECT, "bot", "self"):
        _active_project[update.effective_user.id] = COMMANDER_PROJECT
        await update.message.reply_text(
            "Switched to commander mode."
        )
        return
    if not db.get_project(name):
        await update.message.reply_text(
            f"Unknown project: {name}"
        )
        return
    _active_project[update.effective_user.id] = name
    await update.message.reply_text(
        f"Switched to '{name}'."
    )


@cmd("mcp", "<project> — manage MCP server access")
async def cmd_mcp(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show/toggle MCP servers for a project."""
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /mcp <project>"
        )
        return

    name = context.args[0]
    project = db.get_project(name)
    if not project:
        await update.message.reply_text(
            f"Unknown project: {name}"
        )
        return

    available = get_mcp_servers_for_project(project["cwd"])
    if not available:
        await update.message.reply_text(
            f"No .mcp.json found for '{name}'."
        )
        return

    saved = db.get_project_mcp(name)
    lines = [f"MCP servers for {name}:"]
    buttons = []
    for s in available:
        enabled = saved.get(s, True)
        status = "on" if enabled else "off"
        lines.append(f"  {s}: {status}")
        action = "n" if enabled else "y"
        label = f"Disable {s}" if enabled else f"Enable {s}"
        buttons.append([
            InlineKeyboardButton(
                label,
                callback_data=f"mcp:{action}:{name}:{s}",
            )
        ])

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@cmd("status", "<project> — show session info")
async def cmd_status(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /status <project>"
        )
        return
    name = context.args[0]
    project = db.get_project(name)
    if not project:
        await update.message.reply_text(
            f"Unknown project: {name}"
        )
        return
    session_id = db.get_active_session(name)
    projects = db.list_projects()
    info = next(
        (p for p in projects if p["name"] == name), None
    )
    lines = [
        f"Project: {name}",
        f"Path: {project['cwd']}",
        f"Session: {session_id or 'none'}",
    ]
    if info and info.get("last_used"):
        lines.append(f"Last used: {info['last_used']}")
    await update.message.reply_text("\n".join(lines))


@cmd("reset", "<project> — clear session")
async def cmd_reset(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /reset <project>"
        )
        return
    name = context.args[0]
    if db.reset_session(name):
        reset_memory(name)
        await update.message.reply_text(
            f"Session cleared for '{name}'."
        )
    else:
        await update.message.reply_text(
            f"No active session for '{name}'."
        )


@cmd("history", "<project> — past sessions")
async def cmd_history(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /history <project>"
        )
        return
    name = context.args[0]
    sessions = db.get_session_history(name)
    if not sessions:
        await update.message.reply_text(
            f"No sessions for '{name}'."
        )
        return
    lines = []
    for s in sessions:
        active = " (active)" if s["active"] else ""
        lines.append(
            f"• {s['session_id'][:12]}...{active}\n"
            f"  {s['created_at']}"
        )
    await update.message.reply_text("\n\n".join(lines))


@cmd("permissions", "list/revoke saved permissions")
async def cmd_permissions(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_admin(update):
        return
    if context.args and context.args[0] == "revoke":
        if len(context.args) < 2:
            await update.message.reply_text(
                "Usage: /permissions revoke <tool>"
            )
            return
        tool = context.args[1]
        if db.revoke_tool(tool):
            await update.message.reply_text(
                f"Revoked: {tool}"
            )
        else:
            await update.message.reply_text(
                f"Not found: {tool}"
            )
        return

    tools = db.list_allowed_tools()
    if not tools:
        await update.message.reply_text(
            "No saved permissions. Tap 'Always' to save."
        )
        return
    lines = ["Allowed tools:"]
    for t in tools:
        lines.append(f"  {t}")
    lines.append("\n/permissions revoke <name>")
    await update.message.reply_text("\n".join(lines))


@cmd("update", "check for updates and restart")
async def cmd_update(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_admin(update):
        return

    bot_dir = Path(__file__).parent
    msg = await update.message.reply_text("🔍 Checking for updates…")

    # Fetch latest refs
    proc = await asyncio.create_subprocess_exec(
        "git", "fetch", "origin",
        cwd=bot_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        await msg.edit_text(
            f"git fetch failed:\n<pre>{html.escape(stderr.decode()[:500])}</pre>",
            parse_mode="HTML",
        )
        return

    # Count commits we're behind
    proc = await asyncio.create_subprocess_exec(
        "git", "rev-list", "HEAD..FETCH_HEAD", "--count",
        cwd=bot_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    try:
        count = int(stdout.decode().strip())
    except ValueError:
        count = 0

    if count == 0:
        await msg.edit_text("✅ Already up to date.")
        return

    # Show changelog
    proc = await asyncio.create_subprocess_exec(
        "git", "log", "HEAD..FETCH_HEAD",
        "--oneline", "--no-decorate",
        cwd=bot_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    changelog = stdout.decode().strip()

    lines = [
        f"🆕 <b>{count} update(s) available</b>",
        "",
        f"<pre>{html.escape(changelog[:800])}</pre>",
    ]
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "⬆️ Update & Restart",
            callback_data="update:confirm",
        ),
        InlineKeyboardButton(
            "✖ Cancel",
            callback_data="update:cancel",
        ),
    ]])
    await msg.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=buttons,
    )


@cmd("feedback", "<text> | list | done <id> | rm <id>")
async def cmd_feedback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_admin(update):
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /feedback <text> | list | done/rm <id>"
        )
        return

    sub = args[0]
    if sub == "list":
        items = db.list_feedback()
        if not items:
            await update.message.reply_text(
                "No feedback yet."
            )
            return
        lines = []
        for f in items:
            mark = (
                "[done]" if f["status"] == "done"
                else "[open]"
            )
            lines.append(
                f"#{f['id']} {mark} {f['message']}"
            )
        await update.message.reply_text("\n".join(lines))
        return

    if sub in ("done", "rm") and len(args) >= 2:
        try:
            fid = int(args[1])
        except ValueError:
            await update.message.reply_text("Invalid ID.")
            return
        if sub == "done":
            ok = db.resolve_feedback(fid)
        else:
            ok = db.delete_feedback(fid)
        label = "done" if sub == "done" else "deleted"
        msg = f"#{fid} {label}." if ok else f"#{fid} not found."
        await update.message.reply_text(msg)
        return

    message = " ".join(args)
    fid = db.add_feedback(message)
    await update.message.reply_text(f"Feedback #{fid} saved.")


@cmd("heartbeat", "on|off — toggle inactivity watchdog")
async def cmd_heartbeat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_admin(update):
        return
    global _heartbeat_enabled
    args = context.args
    if not args:
        state = "on" if _heartbeat_enabled else "off"
        active = len(_clients)
        await update.message.reply_text(
            f"Heartbeat is <b>{state}</b>\n"
            f"Inactivity warn: {INACTIVITY_TIMEOUT}s\n"
            f"Hard timeout: {PROMPT_TIMEOUT}s\n"
            f"Idle disconnect: {IDLE_DISCONNECT}s\n"
            f"Active clients: {active}",
            parse_mode="HTML",
        )
        return
    val = args[0].lower()
    if val in ("on", "1", "true", "yes"):
        _heartbeat_enabled = True
        await update.message.reply_text("Heartbeat enabled.")
    elif val in ("off", "0", "false", "no"):
        _heartbeat_enabled = False
        await update.message.reply_text(
            "Heartbeat disabled (no inactivity/timeout checks)."
        )
    else:
        await update.message.reply_text("Usage: /heartbeat on|off")


# --- Shared prompt runner with cancel button ---


def _make_heartbeat(
    project_name: str,
    status_msg,
    buttons,
    last_status: list[str],
    _last_edit: list[float],
    stop_event: asyncio.Event,
    start_time: float,
):
    """Create a heartbeat coroutine with inactivity and timeout detection."""

    async def _edit(text: str) -> None:
        now = asyncio.get_event_loop().time()
        if now - _last_edit[0] < 3.0:
            return
        _last_edit[0] = now
        try:
            await status_msg.edit_text(
                text, parse_mode="HTML", reply_markup=buttons,
            )
        except RetryAfter as e:
            _last_edit[0] = now + e.retry_after
        except Exception:
            pass

    async def heartbeat() -> None:
        inactivity_warned = False
        while not stop_event.is_set():
            await asyncio.sleep(10)
            if stop_event.is_set():
                break
            now = asyncio.get_event_loop().time()
            elapsed = int(now - start_time)
            mins, secs = divmod(elapsed, 60)
            elapsed_str = (
                f"{mins}m {secs}s" if mins else f"{secs}s"
            )

            # Skip inactivity/timeout while waiting for permission
            perm_pending = is_awaiting_permission(project_name)

            suffix = ""
            if perm_pending:
                suffix = "\n🔐 <i>Waiting for permission…</i>"
            elif _heartbeat_enabled:
                # Check inactivity (no SDK messages)
                last_act = get_last_activity(project_name)
                idle_secs = (
                    int(time.monotonic() - last_act)
                    if last_act else elapsed
                )

                if (
                    idle_secs >= INACTIVITY_TIMEOUT
                    and not inactivity_warned
                ):
                    inactivity_warned = True
                    idle_m, idle_s = divmod(idle_secs, 60)
                    suffix = (
                        f"\n⚠️ <b>No activity for "
                        f"{idle_m}m {idle_s}s</b>"
                        f" — task may be stuck"
                    )
                    logger.warning(
                        "[%s] Inactivity: %ds without SDK msgs",
                        project_name, idle_secs,
                    )

            # Hard timeout — auto-kill (skip if permission pending)
            if (
                _heartbeat_enabled
                and not perm_pending
                and elapsed >= PROMPT_TIMEOUT
            ):
                logger.warning(
                    "[%s] Hard timeout after %ds — killing",
                    project_name, elapsed,
                )
                await cancel_running(project_name)
                kill_msg = (
                    f"⏰ <b>{html.escape(project_name)}</b> "
                    f"killed after {mins}m {secs}s "
                    f"(timeout: {PROMPT_TIMEOUT}s)"
                )
                retry_buttons = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "Retry",
                        callback_data=f"retry:{project_name}",
                    ),
                ]])
                try:
                    await status_msg.edit_text(
                        kill_msg, parse_mode="HTML",
                        reply_markup=retry_buttons,
                    )
                except Exception:
                    pass
                return

            current = (
                last_status[0] if last_status else "Clauding…"
            )
            text = (
                f"{current}\n<i>({elapsed_str} elapsed)</i>"
                f"{suffix}"
            )
            last_status[:] = [text]
            await _edit(text)

    return heartbeat


async def _run_and_reply(
    update: Update, project_name: str, prompt: str
) -> None:
    """Send status with Cancel button, run prompt, reply."""
    _last_prompt[project_name] = prompt
    cancel_id = uuid.uuid4().hex[:8]
    preview = html.escape(
        prompt[:60] + ("…" if len(prompt) > 60 else "")
    )
    base = (
        f"⏳ <b>{html.escape(project_name)}</b> · {preview}"
    )

    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Cancel",
            callback_data=f"cancel:{project_name}:{cancel_id}",
        ),
        InlineKeyboardButton(
            "Switch project",
            callback_data="switchmenu:",
        ),
    ]])

    status_msg = await update.message.reply_text(
        f"{base}\n\nClauding…", parse_mode="HTML", reply_markup=buttons,
    )

    if ASYNC_FEEDBACK:
        result_dict = await run_prompt_queued(project_name, prompt, None)
    else:
        last_status: list[str] = []
        _last_edit: list[float] = [0.0]

        async def on_status(label: str) -> None:
            text = f"{base}\n\n{html.escape(label)}"
            if last_status and last_status[0] == text:
                return
            last_status[:] = [text]
            now = asyncio.get_event_loop().time()
            if now - _last_edit[0] < 3.0:
                return
            _last_edit[0] = now
            try:
                await status_msg.edit_text(
                    text, parse_mode="HTML", reply_markup=buttons,
                )
            except RetryAfter as e:
                _last_edit[0] = now + e.retry_after
            except Exception:
                pass

        stop_heartbeat = asyncio.Event()
        start_time = asyncio.get_event_loop().time()
        hb = _make_heartbeat(
            project_name, status_msg, buttons,
            last_status, _last_edit, stop_heartbeat, start_time,
        )
        heartbeat_task = asyncio.create_task(hb())

        result_dict = await run_prompt_queued(
            project_name, prompt, on_status
        )

        stop_heartbeat.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

    try:
        await status_msg.delete()
    except Exception:
        pass

    result_text = result_dict.get("text", "")
    suggested_actions = result_dict.get("actions", [])

    if result_text == "Cancelled.":
        return

    await _send_result(
        update, result_text, project_name, suggested_actions
    )
    await _send_files_from_result(update, result_text)


# Need uuid for cancel IDs
import uuid  # noqa: E402


# --- Callbacks ---


async def callback_switch(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    cq = update.callback_query
    await cq.answer()
    name = cq.data.removeprefix("switch:")
    if name == COMMANDER_PROJECT:
        _active_project[cq.from_user.id] = COMMANDER_PROJECT
        await cq.edit_message_text(
            "Switched to commander mode."
        )
        return
    if not db.get_project(name):
        await cq.edit_message_text(
            f"Project '{name}' no longer exists."
        )
        return
    _active_project[cq.from_user.id] = name
    await cq.edit_message_text(f"Switched to '{name}'.")


async def callback_switchmenu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Cancel current task and show project switcher."""
    cq = update.callback_query
    if cq.from_user.id != ADMIN_CHAT_ID:
        await cq.answer("Unauthorized")
        return

    current = _active_project.get(cq.from_user.id)
    if current:
        await cancel_running(current)
        await cq.answer("Interrupted")
    else:
        await cq.answer()

    projects = db.list_projects()
    if not projects:
        await cq.message.reply_text("No projects registered.")
        return
    buttons = [
        InlineKeyboardButton(
            p["name"], callback_data=f"switch:{p['name']}"
        )
        for p in projects
    ]
    markup = InlineKeyboardMarkup([buttons])
    try:
        await cq.edit_message_text("Switch to:", reply_markup=markup)
    except BadRequest:
        await cq.message.reply_text("Switch to:", reply_markup=markup)


async def callback_permission(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    cq = update.callback_query
    if cq.from_user.id != ADMIN_CHAT_ID:
        await cq.answer("Unauthorized")
        return

    parts = cq.data.split(":", 3)
    if len(parts) < 3:
        await cq.answer("Invalid")
        return

    action = parts[1]
    request_id = parts[2]

    logger.info("callback_permission: action=%s id=%s", action, request_id)

    if action == "a" and len(parts) == 4:
        tool_name = parts[3]
        db.allow_tool(tool_name)
        resolved = resolve_permission(request_id, True)
        if resolved:
            await cq.answer(f"Always: {tool_name}")
            original = cq.message.text or ""
            await cq.edit_message_text(
                f"{original}\n\n-> Always Allowed"
            )
        else:
            await cq.answer("Expired")
    else:
        allowed = action == "y"
        resolved = resolve_permission(request_id, allowed)
        if resolved:
            label = "Allowed" if allowed else "Denied"
            await cq.answer(label)
            original = cq.message.text or ""
            await cq.edit_message_text(
                f"{original}\n\n-> {label}"
            )
        else:
            await cq.answer("Expired")


async def callback_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle Cancel button press."""
    cq = update.callback_query
    if cq.from_user.id != ADMIN_CHAT_ID:
        await cq.answer("Unauthorized")
        return

    # cancel:<project>:<id>
    parts = cq.data.split(":", 2)
    if len(parts) < 2:
        await cq.answer("Invalid")
        return

    project_name = parts[1]
    logger.info("Cancel button pressed for project: %s", project_name)
    if await cancel_running(project_name):
        await cq.answer("Cancelled")
        retry_buttons = None
        if _last_prompt.get(project_name):
            retry_buttons = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "Retry",
                    callback_data=f"retry:{project_name}",
                ),
            ]])
        try:
            await cq.edit_message_text(
                f"Cancelled {project_name}.",
                reply_markup=retry_buttons,
            )
        except BadRequest:
            pass  # message already deleted — nothing to update
    else:
        await cq.answer("Nothing to cancel")


async def callback_retry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Retry the last prompt for a project after timeout/cancel."""
    cq = update.callback_query
    if cq.from_user.id != ADMIN_CHAT_ID:
        await cq.answer("Unauthorized")
        return

    project_name = cq.data.removeprefix("retry:")
    prompt = _last_prompt.get(project_name)
    if not prompt:
        await cq.answer("No prompt to retry")
        return

    if is_project_busy(project_name):
        await cq.answer("Project is busy")
        return

    await cq.answer(f"Retrying on {project_name}")
    try:
        await cq.edit_message_reply_markup(None)
    except Exception:
        pass

    _active_project[cq.from_user.id] = project_name

    # Run prompt using same pattern as callback_quick_reply
    cancel_id = uuid.uuid4().hex[:8]
    preview = html.escape(
        prompt[:60] + ("…" if len(prompt) > 60 else "")
    )
    base = f"🔄 <b>{html.escape(project_name)}</b> · {preview}"
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Cancel",
            callback_data=f"cancel:{project_name}:{cancel_id}",
        ),
    ]])
    status_msg = await cq.message.reply_text(
        f"{base}\n\nClauding…",
        parse_mode="HTML", reply_markup=buttons,
    )

    if ASYNC_FEEDBACK:
        result_dict = await run_prompt_queued(
            project_name, prompt, None
        )
    else:
        last_status: list[str] = []
        _last_edit_t: list[float] = [0.0]

        async def on_status(label: str) -> None:
            text = f"{base}\n\n{html.escape(label)}"
            if last_status and last_status[0] == text:
                return
            last_status[:] = [text]
            now = asyncio.get_event_loop().time()
            if now - _last_edit_t[0] < 3.0:
                return
            _last_edit_t[0] = now
            try:
                await status_msg.edit_text(
                    text, parse_mode="HTML",
                    reply_markup=buttons,
                )
            except RetryAfter as e:
                _last_edit_t[0] = now + e.retry_after
            except Exception:
                pass

        stop_heartbeat = asyncio.Event()
        start_time = asyncio.get_event_loop().time()
        hb = _make_heartbeat(
            project_name, status_msg, buttons,
            last_status, _last_edit_t,
            stop_heartbeat, start_time,
        )
        heartbeat_task = asyncio.create_task(hb())

        result_dict = await run_prompt_queued(
            project_name, prompt, on_status
        )

        stop_heartbeat.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

    try:
        await status_msg.delete()
    except Exception:
        pass

    result_text = result_dict.get("text", "")
    suggested_actions = result_dict.get("actions", [])

    formatted = format_html(result_text)
    chunks = split_message(formatted)
    qr_buttons = _build_quick_replies(
        suggested_actions, project_name
    )

    for chunk in chunks[:-1]:
        try:
            await cq.message.reply_text(
                chunk, parse_mode="HTML"
            )
        except Exception:
            await cq.message.reply_text(
                strip_markdown(chunk)
            )

    last = chunks[-1] if chunks else "(no output)"
    try:
        await cq.message.reply_text(
            last, parse_mode="HTML",
            reply_markup=qr_buttons,
        )
    except Exception:
        await cq.message.reply_text(
            strip_markdown(last),
            reply_markup=qr_buttons,
        )


async def callback_scan(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle scan result button press (single or bulk add)."""
    cq = update.callback_query
    if cq.from_user.id != ADMIN_CHAT_ID:
        await cq.answer("Unauthorized")
        return

    name = cq.data.removeprefix("scan:")
    results = context.bot_data.get("scan_results", {})

    if not results:
        await cq.answer("Scan expired, run /scan again")
        return

    # Bulk add all
    if name == "__all__":
        added = []
        for pname, project in results.items():
            db.add_project(
                pname, project["path"], project["description"]
            )
            added.append(pname)
        context.bot_data["scan_results"] = {}
        await cq.answer(f"Added {len(added)} projects")
        await cq.edit_message_text(
            f"Added {len(added)} projects:\n"
            + "\n".join(f"• {n}" for n in added)
        )
        return

    # Single add
    project = results.get(name)
    if not project:
        await cq.answer("Already added or expired")
        return

    db.add_project(
        name, project["path"], project["description"]
    )
    results.pop(name, None)
    await cq.answer(f"Added: {name}")
    await cq.edit_message_text(
        f"Added '{name}' at {project['path']}"
    )


async def callback_mcp(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle MCP allow/deny/all button presses."""
    cq = update.callback_query
    if cq.from_user.id != ADMIN_CHAT_ID:
        await cq.answer("Unauthorized")
        return

    # mcp:y:<project>:<server> or mcp:n:<project>:<server>
    # mcp:all:<project>
    parts = cq.data.split(":", 3)
    if len(parts) < 3:
        await cq.answer("Invalid")
        return

    action = parts[1]
    project_name = parts[2]

    if action == "all":
        servers = get_mcp_servers_for_project(
            db.get_project(project_name)["cwd"]
        )
        for s in servers:
            db.set_project_mcp(project_name, s, True)
        reset_memory(project_name)
        await cq.answer("All MCP servers enabled")
        await cq.edit_message_text(
            f"All {len(servers)} MCP servers enabled "
            f"for {project_name}."
        )
        return

    if len(parts) < 4:
        await cq.answer("Invalid")
        return

    server_name = parts[3]
    allowed = action == "y"
    db.set_project_mcp(project_name, server_name, allowed)
    reset_memory(project_name)

    label = "Enabled" if allowed else "Disabled"
    await cq.answer(f"{label}: {server_name}")

    # Update message to reflect new state
    project = db.get_project(project_name)
    if project:
        available = get_mcp_servers_for_project(
            project["cwd"]
        )
        saved = db.get_project_mcp(project_name)
        lines = [f"MCP servers for {project_name}:"]
        buttons = []
        for s in available:
            enabled = saved.get(s, True)
            status = "on" if enabled else "off"
            lines.append(f"  {s}: {status}")
            act = "n" if enabled else "y"
            btn_label = (
                f"Disable {s}" if enabled
                else f"Enable {s}"
            )
            buttons.append([
                InlineKeyboardButton(
                    btn_label,
                    callback_data=(
                        f"mcp:{act}:{project_name}:{s}"
                    ),
                )
            ])
        await cq.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def callback_update(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle Update & Restart / Cancel button."""
    cq = update.callback_query
    if cq.from_user.id != ADMIN_CHAT_ID:
        await cq.answer("Unauthorized")
        return

    action = cq.data.split(":", 1)[1]

    if action == "cancel":
        await cq.answer("Cancelled")
        await cq.edit_message_text("Update cancelled.")
        return

    await cq.answer("Pulling…")
    await cq.edit_message_text("⬆️ Pulling updates…")

    bot_dir = Path(__file__).parent

    proc = await asyncio.create_subprocess_exec(
        "git", "pull", "--ff-only",
        cwd=bot_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    pull_out = (stdout + stderr).decode().strip()

    if proc.returncode != 0:
        await cq.message.reply_text(
            f"❌ git pull failed:\n"
            f"<pre>{html.escape(pull_out[:500])}</pre>",
            parse_mode="HTML",
        )
        return

    await cq.edit_message_text(
        f"✅ Pulled.\n<pre>{html.escape(pull_out[:400])}</pre>"
        "\n\nSyncing deps…",
        parse_mode="HTML",
    )

    # uv sync — skip silently if uv not available or in Docker
    if (bot_dir / "pyproject.toml").exists():
        proc = await asyncio.create_subprocess_exec(
            "uv", "sync",
            cwd=bot_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    await cq.message.reply_text("♻️ Restarting…")

    # Release PID file so the new process can acquire it
    _PID_FILE.unlink(missing_ok=True)

    # Re-exec: replace current process with a fresh one
    os.execv(
        sys.executable,
        [sys.executable, str(Path(__file__).resolve())],
    )


async def callback_quick_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle quick-reply button press."""
    cq = update.callback_query
    if cq.from_user.id != ADMIN_CHAT_ID:
        await cq.answer("Unauthorized")
        return

    # qr:<project>:<action>
    parts = cq.data.split(":", 2)
    if len(parts) < 3:
        await cq.answer("Invalid")
        return

    project_name = parts[1]
    action = parts[2]

    # Remove buttons immediately to prevent double-click
    try:
        await cq.edit_message_reply_markup(None)
    except Exception:
        pass

    # Reject if project is already busy
    if is_project_busy(project_name):
        await cq.answer("Project is busy, wait for it to finish.")
        return

    # Use action as prompt: "Yes, <action>"
    prompt = f"Yes, {action.lower()}."
    await cq.answer(f"Running: {prompt[:30]}")

    _active_project[cq.from_user.id] = project_name

    cancel_id = uuid.uuid4().hex[:8]
    preview = html.escape(
        prompt[:60] + ("…" if len(prompt) > 60 else "")
    )
    base = (
        f"⏳ <b>{html.escape(project_name)}</b> · {preview}"
    )
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Cancel",
            callback_data=(
                f"cancel:{project_name}:{cancel_id}"
            ),
        )
    ]])
    status_msg = await cq.message.reply_text(
        f"{base}\n\nClauding…", parse_mode="HTML", reply_markup=buttons,
    )

    if ASYNC_FEEDBACK:
        result_dict = await run_prompt_queued(project_name, prompt, None)
    else:
        last_status: list[str] = []
        _last_edit: list[float] = [0.0]

        async def on_status(label: str) -> None:
            text = f"{base}\n\n{html.escape(label)}"
            if last_status and last_status[0] == text:
                return
            last_status[:] = [text]
            now = asyncio.get_event_loop().time()
            if now - _last_edit[0] < 3.0:
                return
            _last_edit[0] = now
            try:
                await status_msg.edit_text(
                    text, parse_mode="HTML", reply_markup=buttons,
                )
            except RetryAfter as e:
                _last_edit[0] = now + e.retry_after
            except Exception:
                pass

        stop_heartbeat = asyncio.Event()
        start_time = asyncio.get_event_loop().time()
        hb = _make_heartbeat(
            project_name, status_msg, buttons,
            last_status, _last_edit, stop_heartbeat, start_time,
        )
        heartbeat_task = asyncio.create_task(hb())

        result_dict = await run_prompt_queued(
            project_name, prompt, on_status
        )

        stop_heartbeat.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

    try:
        await status_msg.delete()
    except Exception:
        pass

    result_text = result_dict.get("text", "")
    suggested_actions = result_dict.get("actions", [])

    formatted = format_html(result_text)
    chunks = split_message(formatted)
    qr_buttons = _build_quick_replies(
        suggested_actions, project_name
    )

    for chunk in chunks[:-1]:
        try:
            await cq.message.reply_text(
                chunk, parse_mode="HTML"
            )
        except Exception:
            await cq.message.reply_text(
                strip_markdown(chunk)
            )

    last = chunks[-1] if chunks else "(no output)"
    try:
        await cq.message.reply_text(
            last, parse_mode="HTML",
            reply_markup=qr_buttons,
        )
    except Exception:
        await cq.message.reply_text(
            strip_markdown(last),
            reply_markup=qr_buttons,
        )

    # Send any files Claude created
    await _send_files_from_result(cq.message, result_text)


# --- Commander meta-project (bot self-management) ---

COMMANDER_PROJECT = "commander"

_CMD_INTENTS = [
    (r"\b(?:list|show|all)\s*projects?\b", "list_projects"),
    (r"\bscan\b", "scan"),
    (r"\badd\s+(?:all|everything)\b", "add_all"),
    (r"\badd\s+(\S+)", "add_one"),
    (r"\bremove\s+(\S+)", "remove"),
    (r"\bstatus\b", "status"),
    (r"\breset\s+(\S+)", "reset"),
    (r"\bheartbeat\s*(on|off)?\b", "heartbeat"),
    (r"\bpermissions?\b", "permissions"),
    (r"\bhelp\b", "help"),
]


async def _handle_commander(
    update: Update, prompt: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle text when switched to the commander meta-project."""
    global _heartbeat_enabled
    text = prompt.strip().lower()

    for pattern, intent in _CMD_INTENTS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            break
    else:
        intent = None
        m = None

    if intent == "list_projects":
        projects = db.list_projects()
        if not projects:
            await update.message.reply_text("No projects registered.")
            return
        lines = []
        for p in projects:
            name = p["name"]
            active = " (active)" if name in _clients else ""
            lines.append(f"• <b>{html.escape(name)}</b>{active}")
        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML"
        )

    elif intent == "scan":
        found = scan_projects()
        existing = {p["cwd"] for p in db.list_projects()}
        new = [p for p in found if p["path"] not in existing]
        if not new:
            await update.message.reply_text(
                f"Found {len(found)} projects, all registered."
            )
            return
        buttons = []
        for p in new[:20]:
            markers = ", ".join(p["markers"][:3])
            buttons.append([InlineKeyboardButton(
                f"{p['name']} ({markers})",
                callback_data=f"scan:{p['name']}",
            )])
        if len(new) > 1:
            buttons.append([InlineKeyboardButton(
                f"Add all {len(new)} projects",
                callback_data="scan:__all__",
            )])
        context.bot_data["scan_results"] = {
            p["name"]: p for p in new
        }
        await update.message.reply_text(
            f"Found {len(new)} new projects:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif intent == "add_all":
        results = context.bot_data.get("scan_results", {})
        if not results:
            await update.message.reply_text(
                "Nothing to add. Say 'scan' first."
            )
            return
        added = []
        for pname, project in results.items():
            db.add_project(
                pname, project["path"], project["description"]
            )
            added.append(pname)
        context.bot_data["scan_results"] = {}
        await update.message.reply_text(
            f"Added {len(added)} projects:\n"
            + "\n".join(f"• {n}" for n in added)
        )

    elif intent == "add_one" and m:
        name = m.group(1)
        results = context.bot_data.get("scan_results", {})
        project = results.get(name)
        if project:
            db.add_project(
                name, project["path"], project["description"]
            )
            results.pop(name, None)
            await update.message.reply_text(f"Added '{name}'.")
        else:
            await update.message.reply_text(
                f"'{name}' not in scan results. "
                "Use 'scan' first or /add <name> <path>."
            )

    elif intent == "remove" and m:
        name = m.group(1)
        if db.remove_project(name):
            await update.message.reply_text(f"Removed '{name}'.")
        else:
            await update.message.reply_text(
                f"'{name}' not found."
            )

    elif intent == "status":
        projects = db.list_projects()
        active = len(_clients)
        busy = [
            n for n in _running_tasks
            if not _running_tasks[n].done()
        ]
        lines = [
            f"Projects: {len(projects)}",
            f"Active clients: {active}",
            f"Busy: {', '.join(busy) if busy else 'none'}",
            f"Heartbeat: {'on' if _heartbeat_enabled else 'off'}",
        ]
        await update.message.reply_text("\n".join(lines))

    elif intent == "reset" and m:
        name = m.group(1)
        if db.get_project(name):
            reset_memory(name)
            await update.message.reply_text(
                f"Session reset for '{name}'."
            )
        else:
            await update.message.reply_text(
                f"'{name}' not found."
            )

    elif intent == "heartbeat":
        val = m.group(1) if m and m.group(1) else None
        if val == "on":
            _heartbeat_enabled = True
            await update.message.reply_text("Heartbeat enabled.")
        elif val == "off":
            _heartbeat_enabled = False
            await update.message.reply_text("Heartbeat disabled.")
        else:
            state = "on" if _heartbeat_enabled else "off"
            await update.message.reply_text(
                f"Heartbeat is {state}."
            )

    elif intent == "permissions":
        perms = db.list_allowed_tools()
        if not perms:
            await update.message.reply_text("No saved permissions.")
            return
        lines = [f"• <code>{html.escape(p)}</code>" for p in perms]
        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML"
        )

    elif intent == "help":
        await update.message.reply_text(
            "<b>Commander mode</b>\n\n"
            "• list projects\n"
            "• scan\n"
            "• add all / add <name>\n"
            "• remove <name>\n"
            "• status\n"
            "• reset <name>\n"
            "• heartbeat on/off\n"
            "• permissions\n"
            "• switch to <project>\n\n"
            "Or just say what you need.",
            parse_mode="HTML",
        )

    else:
        await update.message.reply_text(
            "I'm in commander mode. "
            "Try: list projects, scan, status, help\n"
            "Or 'switch to <project>' to work on code."
        )


# --- Text handler with auto-routing ---

_GREETINGS = frozenset([
    "hi", "hello", "hey", "yo", "sup", "hola",
    "ping", "test", "ok", "okay", "thanks", "thx", "bye",
])


async def _maybe_handle_simple(
    update: Update, user_id: int, prompt: str
) -> bool:
    """Handle trivial messages instantly without going to Claude.
    Returns True if handled."""
    word = prompt.strip().lower().rstrip("!?.")
    if word not in _GREETINGS:
        return False

    project_name = _get_active_project(user_id)
    if project_name:
        status = f"Ready — active project: <b>{html.escape(project_name)}</b>"
    else:
        projects = db.list_projects()
        if projects:
            status = (
                f"Ready — {len(projects)} project(s) registered. "
                "Use /switch to select one."
            )
        else:
            status = "Ready — no projects yet. Use /add or /scan."

    await update.message.reply_text(status, parse_mode="HTML")
    return True


_SWITCH_RE = re.compile(
    r"^(?:switch|change|use|go to)\s+(?:to\s+|project\s+)?(\S+)\s*$",
    re.IGNORECASE,
)


def _detect_switch_intent(text: str) -> str | None:
    """Return project name if message is a natural-language switch request."""
    m = _SWITCH_RE.match(text.strip())
    return m.group(1) if m else None


async def handle_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_admin(update):
        return

    user_id = update.effective_user.id
    prompt = update.message.text

    # 0. Instant reply for greetings/trivial messages
    if await _maybe_handle_simple(update, user_id, prompt):
        return

    # 1. Natural-language switch intent ("switch to myproject", "use myproject")
    switch_target = _detect_switch_intent(prompt)
    if switch_target:
        # "switch to commander/bot" activates meta-project
        if switch_target.lower() in ("commander", "bot", "self"):
            _active_project[user_id] = COMMANDER_PROJECT
            await update.message.reply_text(
                "Switched to <b>commander</b> mode. "
                "Say 'help' for available commands.",
                parse_mode="HTML",
            )
            return

        projects = db.list_projects()
        names = [p["name"] for p in projects]
        matches = difflib.get_close_matches(
            switch_target.lower(),
            [n.lower() for n in names],
            n=1, cutoff=0.6,
        )
        if matches:
            matched_name = names[
                [n.lower() for n in names].index(matches[0])
            ]
            _active_project[user_id] = matched_name
            await update.message.reply_text(
                f"Switched to <b>{html.escape(matched_name)}</b>.",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                f"Project '{switch_target}' not found. "
                "Use /projects to see registered projects."
            )
        return

    # 2. Commander meta-project — handle locally
    project_name = _get_active_project(user_id)
    if project_name == COMMANDER_PROJECT:
        await _handle_commander(update, prompt, context)
        return

    # 2. Auto-route by description matching
    if not project_name:
        projects = db.list_projects()
        if not projects:
            await update.message.reply_text(
                "No projects. Use /add or /scan first."
            )
            return

        matched = match_project_by_description(
            prompt, projects
        )
        if matched:
            project_name = matched
            logger.info(
                "Auto-routed to %s", project_name
            )
        else:
            # 3. Fall back to most recently used
            active = [
                p for p in projects
                if p.get("session_id")
            ]
            if active:
                active.sort(
                    key=lambda p: p.get("last_used") or "",
                    reverse=True,
                )
                project_name = active[0]["name"]
            else:
                # 4. Just pick first project
                project_name = projects[0]["name"]

    # Stick to this project for subsequent messages
    _active_project[user_id] = project_name

    # Notify if queuing behind a running task
    if is_project_busy(project_name):
        await update.message.reply_text(
            f"⏳ <b>{html.escape(project_name)}</b> is busy "
            "— your message is queued. Press Cancel to abort.",
            parse_mode="HTML",
        )

    await _run_and_reply(update, project_name, prompt)


async def handle_voice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Transcribe voice message via Groq Whisper, then route as prompt."""
    if not is_admin(update):
        return

    if not _groq_client:
        await update.message.reply_text(
            "Voice transcription unavailable — set GROQ_API_KEY."
        )
        return

    status_msg = await update.message.reply_text("🎙 Transcribing...")

    try:
        voice = update.message.voice
        tg_file = await context.bot.get_file(voice.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        buf.seek(0)

        transcription = await _groq_client.audio.transcriptions.create(
            file=("voice.ogg", buf.read()),
            model="whisper-large-v3-turbo",
        )
        transcript = transcription.text.strip()
    except Exception as e:
        await status_msg.edit_text(f"Transcription error: {e}")
        return

    if not transcript:
        await status_msg.edit_text("Could not transcribe audio.")
        return

    await status_msg.edit_text(
        f"🎙 <i>{html.escape(transcript)}</i>",
        parse_mode="HTML",
    )

    user_id = update.effective_user.id
    project_name = _get_active_project(user_id)

    # Commander meta-project — handle locally
    if project_name == COMMANDER_PROJECT:
        await _handle_commander(update, transcript, context)
        return

    if not project_name:
        projects = db.list_projects()
        if not projects:
            await update.message.reply_text(
                "No projects registered. Use /add or /scan first."
            )
            return
        active = [p for p in projects if p.get("session_id")]
        if active:
            active.sort(
                key=lambda p: p.get("last_used") or "", reverse=True
            )
            project_name = active[0]["name"]
        else:
            project_name = projects[0]["name"]

    await _run_and_reply(update, project_name, transcript)


async def _get_active_project_for_update(
    update: Update,
) -> str | None:
    """Return active project for a user, falling back to MRU."""
    user_id = update.effective_user.id
    project_name = _get_active_project(user_id)
    if not project_name:
        projects = db.list_projects()
        if not projects:
            return None
        active = [p for p in projects if p.get("session_id")]
        if active:
            active.sort(
                key=lambda p: p.get("last_used") or "", reverse=True
            )
            project_name = active[0]["name"]
        else:
            project_name = projects[0]["name"]
    return project_name


async def handle_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Download photo, pass path + caption to active project."""
    if not is_admin(update):
        return

    project_name = await _get_active_project_for_update(update)
    if not project_name:
        await update.message.reply_text(
            "No projects registered. Use /add or /scan first."
        )
        return

    photo = update.message.photo[-1]  # largest resolution
    tg_file = await context.bot.get_file(photo.file_id)

    suffix = ".jpg"
    with tempfile.NamedTemporaryFile(
        suffix=suffix, delete=False, dir="/tmp"
    ) as tmp:
        tmp_path = tmp.name

    await tg_file.download_to_drive(tmp_path)

    caption = update.message.caption or ""
    prompt = f"[Image attached: {tmp_path}]"
    if caption:
        prompt += f"\n{caption}"

    await _run_and_reply(update, project_name, prompt)


async def handle_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Download document, pass path + caption to active project."""
    if not is_admin(update):
        return

    project_name = await _get_active_project_for_update(update)
    if not project_name:
        await update.message.reply_text(
            "No projects registered. Use /add or /scan first."
        )
        return

    doc = update.message.document
    file_name = doc.file_name or "file"
    suffix = Path(file_name).suffix or ""
    tg_file = await context.bot.get_file(doc.file_id)

    with tempfile.NamedTemporaryFile(
        suffix=suffix, delete=False, dir="/tmp",
        prefix=f"tg_{Path(file_name).stem}_",
    ) as tmp:
        tmp_path = tmp.name

    await tg_file.download_to_drive(tmp_path)

    caption = update.message.caption or ""
    prompt = f"[File attached: {tmp_path} (original: {file_name})]"
    if caption:
        prompt += f"\n{caption}"

    await _run_and_reply(update, project_name, prompt)


async def handle_unknown_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not is_admin(update):
        return
    typed = (
        update.message.text.split()[0]
        .lstrip("/").split("@")[0]
    )
    known = [name for name, _, _ in COMMANDS]
    matches = difflib.get_close_matches(
        typed, known, n=1, cutoff=0.6
    )
    if matches:
        msg = f"Unknown /{typed}. Did you mean /{matches[0]}?"
    else:
        msg = f"Unknown /{typed}. Use /help"
    await update.message.reply_text(msg)


_PID_FILE = Path(__file__).parent / "data" / "bot.pid"


def _acquire_pid() -> None:
    """Stop old instance if running, then write our PID."""
    import signal

    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _PID_FILE.exists():
        try:
            pid = int(_PID_FILE.read_text().strip())
        except ValueError:
            pid = 0
        if pid and pid != os.getpid():
            try:
                os.kill(pid, 0)  # check existence
                logger.info(
                    "Stopping old instance (PID %d)...", pid
                )
                os.kill(pid, signal.SIGTERM)
                # Give it a moment to shut down
                import time
                time.sleep(2)
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass  # already dead
            except (ProcessLookupError, PermissionError):
                pass  # stale PID file
    _PID_FILE.write_text(str(os.getpid()))


def _release_pid() -> None:
    try:
        _PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")
    if not ADMIN_CHAT_ID:
        raise ValueError("ADMIN_CHAT_ID not set")

    _acquire_pid()

    import atexit
    atexit.register(_release_pid)

    db.init_db()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    # /start alias
    app.add_handler(CommandHandler("start", cmd_help))

    # Registered commands
    for name, handler, _ in COMMANDS:
        app.add_handler(CommandHandler(name, handler))

    # Callbacks (order matters — more specific first)
    app.add_handler(
        CallbackQueryHandler(
            callback_permission, pattern=r"^perm:"
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            callback_cancel, pattern=r"^cancel:"
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            callback_retry, pattern=r"^retry:"
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            callback_scan, pattern=r"^scan:"
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            callback_mcp, pattern=r"^mcp:"
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            callback_update, pattern=r"^update:"
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            callback_quick_reply, pattern=r"^qr:"
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            callback_switchmenu, pattern=r"^switchmenu:"
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            callback_switch, pattern=r"^switch:"
        )
    )

    # Voice messages
    app.add_handler(
        MessageHandler(filters.VOICE, handle_voice)
    )

    # Photos and documents
    app.add_handler(
        MessageHandler(filters.PHOTO, handle_photo)
    )
    app.add_handler(
        MessageHandler(filters.Document.ALL, handle_document)
    )

    # Text and unknown commands
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND, handle_text
        )
    )
    app.add_handler(
        MessageHandler(
            filters.COMMAND, handle_unknown_command
        )
    )

    set_telegram_bot(app.bot, ADMIN_CHAT_ID)

    async def error_handler(update, context):
        """Log errors and suppress Conflict noise."""
        err = context.error
        if "Conflict" in str(err):
            logger.warning("Telegram Conflict — another instance? %s", err)
            return
        logger.error("Unhandled exception: %s", err, exc_info=err)

    app.add_error_handler(error_handler)

    _reaper_task: asyncio.Task | None = None

    async def post_init(application):
        nonlocal _reaper_task
        _reaper_task = asyncio.create_task(idle_reaper())
        logger.info(
            "Ready — lazy connect, idle disconnect after %ds",
            IDLE_DISCONNECT,
        )

        # Onboarding: if no projects registered, scan and offer to add
        if not db.list_projects():
            found = scan_projects()
            if found:
                buttons = []
                for p in found[:20]:
                    markers = ", ".join(p["markers"][:3])
                    label = f"{p['name']} ({markers})"
                    buttons.append([
                        InlineKeyboardButton(
                            label,
                            callback_data=f"scan:{p['name']}",
                        )
                    ])
                if len(found) > 1:
                    buttons.append([
                        InlineKeyboardButton(
                            f"Add all {len(found)} projects",
                            callback_data="scan:__all__",
                        )
                    ])
                application.bot_data["scan_results"] = {
                    p["name"]: p for p in found
                }
                await application.bot.send_message(
                    ADMIN_CHAT_ID,
                    f"Welcome! Found {len(found)} projects. "
                    "Tap to add or use /scan later:",
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
            else:
                await application.bot.send_message(
                    ADMIN_CHAT_ID,
                    "Welcome! No projects found. "
                    "Use /add <name> <path> to register one.",
                )

    async def post_shutdown(application):
        nonlocal _reaper_task
        if _reaper_task and not _reaper_task.done():
            _reaper_task.cancel()
            try:
                await _reaper_task
            except asyncio.CancelledError:
                pass
        names = list(_clients.keys())
        if names:
            logger.info("Disconnecting %d SDK client(s)...", len(names))
            for name in names:
                await disconnect_client(name)

    app.post_init = post_init
    app.post_shutdown = post_shutdown

    logger.info("Claude Commander starting...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=DROP_PENDING,
    )


if __name__ == "__main__":
    main()
