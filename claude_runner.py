"""Claude CLI wrapper — direct subprocess via stream-json protocol."""

import asyncio
import html
import json
import logging
import re
import tempfile
from asyncio.subprocess import PIPE
from pathlib import Path
from typing import Any, Callable

import db

logger = logging.getLogger(__name__)

# Telegram message limit
TG_MAX_LEN = 4096

# Timeout for a single prompt (5 minutes)
PROMPT_TIMEOUT = 300

# Default model
DEFAULT_MODEL = "opus"

# Available models
AVAILABLE_MODELS = ["opus", "sonnet", "haiku"]

# Current active model (can be changed at runtime)
_active_model: str = DEFAULT_MODEL

# Per-project locks (one prompt at a time per project)
_locks: dict[str, asyncio.Lock] = {}

# Running subprocesses per project (for cancel support)
_procs: dict[str, asyncio.subprocess.Process] = {}


def get_model() -> str:
    return _active_model


def set_model(model: str) -> bool:
    global _active_model
    if model not in AVAILABLE_MODELS:
        return False
    _active_model = model
    return True

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


def _extract_label(data: dict) -> str | None:
    """Extract a short status label from a stream-json assistant message."""
    message = data.get("message", {})
    content = message.get("content", [])
    if isinstance(content, str):
        stripped = content.strip()
        if stripped:
            return f"→ {stripped[:60]}"
        return None

    for block in content:
        if block.get("type") != "tool_use":
            continue
        tool = block.get("name", "")
        inp = block.get("input", {})

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

    return None


# --- MCP config ---


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


def _write_mcp_tempfile(mcp_servers: dict) -> Path:
    """Write MCP config to a temp file for --mcp-config flag."""
    data = {"mcpServers": mcp_servers}
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="mcp_",
        delete=False,
    )
    json.dump(data, tmp)
    tmp.close()
    return Path(tmp.name)


# --- Core: subprocess prompt execution ---


async def run_prompt(
    project_name: str,
    prompt: str,
    on_status: Callable[[str], Any] | None = None,
) -> str:
    """Send prompt to claude CLI subprocess, return result."""
    project = db.get_project(project_name)
    if not project:
        return f"Project '{project_name}' not found."

    cwd = project["cwd"]
    session_id = db.get_active_session(project_name) or ""

    # Build CLI command
    cmd = [
        "claude",
        "--output-format", "stream-json",
        "--input-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
        "--model", _active_model,
    ]

    # MCP config
    mcp = _load_filtered_mcp(project_name, cwd)
    mcp_tmpfile = None
    if mcp:
        mcp_tmpfile = _write_mcp_tempfile(mcp)
        cmd += ["--mcp-config", str(mcp_tmpfile)]

    # Resume session if exists
    if session_id:
        cmd += ["--resume", session_id]

    preview = prompt[:80] + ("..." if len(prompt) > 80 else "")
    logger.info("[%s] Prompt: %s", project_name, preview)

    # Add interpretation prefix
    interp = _interpret_prompt(prompt)
    if interp:
        logger.debug("[%s] Interpretation: %s", project_name, interp)
        prompt = f"{interp}\n{prompt}"

    # Acquire per-project lock (queuing)
    lock = _locks.setdefault(project_name, asyncio.Lock())
    async with lock:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE, cwd=cwd,
            )
            _procs[project_name] = proc

            # Send prompt on stdin
            msg = json.dumps({
                "type": "user",
                "session_id": session_id,
                "message": {"role": "user", "content": prompt},
                "parent_tool_use_id": None,
            })
            proc.stdin.write(msg.encode() + b"\n")
            await proc.stdin.drain()
            proc.stdin.close()

            # Read stdout line by line
            result_text = ""
            async for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("[%s] Non-JSON line: %s", project_name, line[:100])
                    continue

                msg_type = data.get("type")
                if msg_type == "result":
                    result_text = data.get("result") or "(no output)"
                    new_sid = data.get("session_id")
                    if new_sid:
                        db.save_session(project_name, new_sid)
                    db.touch_session(project_name)
                    is_error = data.get("is_error", False)
                    if is_error:
                        logger.warning("[%s] Claude returned error", project_name)
                    logger.info(
                        "[%s] Result (%d chars)",
                        project_name, len(result_text),
                    )
                elif msg_type == "assistant" and on_status:
                    label = _extract_label(data)
                    if label:
                        logger.debug("[%s] Event: %s", project_name, label)
                        try:
                            await on_status(label)
                        except Exception:
                            pass

            await proc.wait()
            return result_text or "(no output)"

        except asyncio.CancelledError:
            logger.info("[%s] Cancelled", project_name)
            return "Cancelled."
        except Exception as e:
            logger.exception("[%s] Error: %s", project_name, e)
            return f"Error: {e}"
        finally:
            _procs.pop(project_name, None)
            if mcp_tmpfile:
                try:
                    mcp_tmpfile.unlink(missing_ok=True)
                except Exception:
                    pass


def cancel_running(project_name: str) -> bool:
    """Cancel a running prompt by terminating the subprocess."""
    proc = _procs.pop(project_name, None)
    if proc and proc.returncode is None:
        proc.terminate()
        logger.info("[%s] Subprocess terminated", project_name)
        return True
    return False


def is_running(project_name: str) -> bool:
    """Check if a prompt is currently running for a project."""
    proc = _procs.get(project_name)
    return proc is not None and proc.returncode is None


def reset_memory(project_name: str) -> None:
    """Clear session for a project."""
    db.reset_session(project_name)


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
