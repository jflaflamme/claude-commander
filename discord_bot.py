"""Claude Commander — Discord bot for managing Claude Code sessions."""

import asyncio
import difflib
import io
import logging
import os
import re
import sys
import tempfile
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

import db
from claude_runner import (
    AVAILABLE_MODELS,
    DISCORD_MAX_LEN,
    cancel_running,
    get_model,
    get_mcp_servers_for_project,
    is_running,
    match_project_by_description,
    reset_memory,
    run_prompt,
    scan_projects,
    set_model,
    split_message,
    strip_markdown,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("claude_runner").setLevel(logging.DEBUG)
logging.getLogger("discord").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ADMIN_DISCORD_ID = int(os.getenv("ADMIN_DISCORD_ID", "0"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

try:
    from groq import AsyncGroq as _AsyncGroq
    _groq_client = _AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
except ImportError:
    _groq_client = None


# --- Helpers ---


def is_admin(interaction_or_message) -> bool:
    if isinstance(interaction_or_message, discord.Interaction):
        return interaction_or_message.user.id == ADMIN_DISCORD_ID
    return interaction_or_message.author.id == ADMIN_DISCORD_ID


def _resolve_context_ids(channel: discord.abc.Messageable) -> tuple:
    """Extract thread_id, channel_id, category_id, guild_id from a channel."""
    thread_id = None
    channel_id = None
    category_id = None
    guild_id = None

    if isinstance(channel, discord.Thread):
        thread_id = str(channel.id)
        channel_id = str(channel.parent_id)
        if channel.parent and channel.parent.category_id:
            category_id = str(channel.parent.category_id)
        if channel.guild:
            guild_id = str(channel.guild.id)
    elif isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
        channel_id = str(channel.id)
        if channel.category_id:
            category_id = str(channel.category_id)
        if channel.guild:
            guild_id = str(channel.guild.id)
    elif isinstance(channel, discord.DMChannel):
        channel_id = str(channel.id)

    return thread_id, channel_id, category_id, guild_id


def _resolve_project(channel: discord.abc.Messageable) -> tuple[str | None, str | None]:
    """Resolve project and session from Discord hierarchy. Returns (project_name, session_id)."""
    ids = _resolve_context_ids(channel)
    binding = db.resolve_discord_binding(*ids)
    if binding:
        return binding.get("project_name"), binding.get("session_id")
    return None, None


def _get_binding_scope(channel: discord.abc.Messageable) -> tuple[str, str]:
    """Get the discord_id and scope for the current context (for storing bindings)."""
    if isinstance(channel, discord.Thread):
        return str(channel.id), "thread"
    elif isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
        return str(channel.id), "channel"
    elif isinstance(channel, discord.DMChannel):
        return str(channel.id), "channel"
    return str(channel.id), "channel"


async def _send_result(channel, result: str, project_name: str = "") -> discord.Message | None:
    """Send result, splitting for Discord's 2000 char limit."""
    chunks = split_message(result, max_len=DISCORD_MAX_LEN)

    last_msg = None
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        view = _build_quick_replies(result, project_name) if is_last and project_name else None
        try:
            last_msg = await channel.send(chunk, view=view)
        except discord.HTTPException:
            # Try plain text fallback
            plain = strip_markdown(chunk)
            last_msg = await channel.send(plain[:DISCORD_MAX_LEN], view=view)

    return last_msg


def _build_quick_replies(result: str, project_name: str) -> discord.ui.View | None:
    """Build contextual quick-reply buttons."""
    if not project_name:
        return None

    view = discord.ui.View(timeout=300)
    result_lower = result.lower()
    has_buttons = False

    if re.search(r"\b(error|failed|traceback|exception)\b", result_lower):
        view.add_item(QuickReplyButton("Fix it", project_name, "fix"))
        has_buttons = True
    if re.search(r"\b(created|wrote|edited|modified)\b", result_lower):
        view.add_item(QuickReplyButton("Show diff", project_name, "diff"))
        has_buttons = True
    if re.search(r"\b(run(ning)? tests?|test suite|failing test|pytest|unittest)\b", result_lower):
        view.add_item(QuickReplyButton("Run tests", project_name, "test"))
        has_buttons = True

    return view if has_buttons else None


# Quick-reply prompt mappings
QR_PROMPTS = {
    "fix": "Fix the error mentioned in the previous response.",
    "diff": "Show git diff of recent changes.",
    "test": "Run the tests.",
}


# --- File detection (same as Telegram bot) ---

_FILE_PATH_RE = re.compile(
    r"(?<!\w)((?:/tmp|/home|/root|/var|/data|/opt)"
    r"[\w./\-_ ]+\.(?:pdf|png|jpe?g|gif|webp|csv|xlsx"
    r"|docx|txt|zip|svg|mp4|json|html))",
    re.IGNORECASE,
)


async def _send_files_from_result(channel, result: str) -> None:
    """Detect file paths in result and send as Discord attachments."""
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
        try:
            await channel.send(file=discord.File(str(p)))
        except Exception as e:
            logger.warning("Failed to send file %s: %s", path, e)


# --- UI Components ---


class QuickReplyButton(discord.ui.Button):
    def __init__(self, label: str, project_name: str, action: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.project_name = project_name
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return

        prompt = QR_PROMPTS.get(self.action, self.action)
        await interaction.response.send_message(f"Running: {prompt[:60]}...")

        # Disable buttons on original message
        self.view.stop()
        try:
            await interaction.message.edit(view=None)
        except Exception:
            pass

        await _run_and_reply(interaction.channel, self.project_name, prompt)


class CancelButton(discord.ui.View):
    def __init__(self, project_name: str):
        super().__init__(timeout=600)
        self.project_name = project_name

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        if cancel_running(self.project_name):
            await interaction.response.send_message(f"Cancelled {self.project_name}.")
        else:
            await interaction.response.send_message("Nothing to cancel.", ephemeral=True)
        self.stop()


class ProjectSelectView(discord.ui.View):
    """Dropdown to select a project."""
    def __init__(self, projects: list[dict], action: str = "switch"):
        super().__init__(timeout=120)
        self.action = action
        options = [
            discord.SelectOption(label=p["name"], description=(p.get("description") or "")[:100])
            for p in projects[:25]
        ]
        self.select = discord.ui.Select(placeholder="Select a project...", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        if not is_admin(interaction):
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        name = self.select.values[0]
        if self.action == "switch":
            discord_id, scope = _get_binding_scope(interaction.channel)
            db.set_discord_binding(discord_id, scope, project_name=name)
            await interaction.response.send_message(f"Switched to **{name}**.")
        elif self.action == "scan_add":
            # handled in scan command
            pass
        self.stop()


# --- Prompt runner ---


async def _run_and_reply(channel, project_name: str, prompt: str) -> None:
    """Send status message, run prompt, reply with result."""
    preview = prompt[:60] + ("..." if len(prompt) > 60 else "")
    cancel_view = CancelButton(project_name)
    status_msg = await channel.send(
        f"**{project_name}** | {preview}\n\nClauding...",
        view=cancel_view,
    )

    # Hung check — one edit after 2 min
    hung_edited = False
    HUNG_TIMEOUT = 120

    async def hung_check():
        nonlocal hung_edited
        await asyncio.sleep(HUNG_TIMEOUT)
        if is_running(project_name) and not hung_edited:
            hung_edited = True
            try:
                await status_msg.edit(content=f"**{project_name}** | {preview}\n\nStill working (2m+)...")
            except Exception:
                pass

    hung_task = asyncio.create_task(hung_check())
    result = await run_prompt(project_name, prompt)
    hung_task.cancel()
    try:
        await hung_task
    except asyncio.CancelledError:
        pass

    # Delete status message
    try:
        await status_msg.delete()
    except Exception:
        pass

    cancel_view.stop()

    if result == "Cancelled.":
        return

    # Store session if this context doesn't have one yet
    discord_id, scope = _get_binding_scope(channel)
    binding = db.get_discord_binding(discord_id)
    if binding:
        # Update session from the project's latest
        session_id = db.get_active_session(project_name)
        if session_id:
            db.update_discord_session(discord_id, session_id)

    await _send_result(channel, result, project_name)
    await _send_files_from_result(channel, result)


# --- Bot setup ---


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


@bot.event
async def on_ready():
    logger.info("Discord bot ready as %s", bot.user)
    try:
        synced = await bot.tree.sync()
        logger.info("Synced %d slash commands", len(synced))
    except Exception as e:
        logger.error("Failed to sync commands: %s", e)


# --- Slash commands ---


@bot.tree.command(name="help", description="Show available commands")
async def cmd_help(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("Unauthorized.", ephemeral=True)
        return
    lines = [
        "**Claude Commander**\n",
        "`/help` — show this help",
        "`/projects` — list registered projects",
        "`/add` — register a project",
        "`/scan` — find and add projects",
        "`/remove` — unregister a project",
        "`/edit` — update project path/description",
        "`/ask` — send prompt to a project",
        "`/bind` — bind this channel/thread to a project",
        "`/unbind` — remove binding for this context",
        "`/bindings` — show all bindings in this server",
        "`/mcp` — manage MCP server access",
        "`/status` — show session info",
        "`/reset` — clear session",
        "`/history` — past sessions",
        "`/model` — show or switch model",
        "`/feedback` — leave feedback",
        "",
        "Or just type a message — it routes to the bound/active project.",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="projects", description="List registered projects")
async def cmd_projects(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("Unauthorized.", ephemeral=True)
        return
    projects = db.list_projects()
    if not projects:
        await interaction.response.send_message("No projects registered. Use `/add` or `/scan`.")
        return

    # Check what's bound here
    project_name, _ = _resolve_project(interaction.channel)
    lines = []
    for p in projects:
        status = "active" if p.get("session_id") else "idle"
        desc = f" — {p['description']}" if p.get("description") else ""
        cur = " **\\***" if p["name"] == project_name else ""
        lines.append(f"- **{p['name']}** [{status}]{desc}{cur}\n  `{p['cwd']}`")

    view = ProjectSelectView(projects)
    await interaction.response.send_message(
        "\n".join(lines) + "\n\n(\\* = bound here)\nSelect to switch:",
        view=view,
    )


@bot.tree.command(name="add", description="Register a project")
@app_commands.describe(name="Project name", path="Path to project directory", description="Project description")
async def cmd_add(interaction: discord.Interaction, name: str, path: str, description: str = ""):
    if not is_admin(interaction):
        await interaction.response.send_message("Unauthorized.", ephemeral=True)
        return
    cwd = os.path.expanduser(path)
    if not os.path.isdir(cwd):
        await interaction.response.send_message(f"Directory not found: `{cwd}`")
        return
    db.add_project(name, cwd, description)
    msg = f"Project **{name}** registered at `{cwd}`"

    # Check for MCP servers
    servers = get_mcp_servers_for_project(cwd)
    if servers:
        msg += f"\n\nFound {len(servers)} MCP server(s): {', '.join(servers)}\nUse `/mcp {name}` to configure."

    await interaction.response.send_message(msg)


@bot.tree.command(name="scan", description="Find projects and add them")
async def cmd_scan(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("Unauthorized.", ephemeral=True)
        return

    await interaction.response.defer()
    found = scan_projects()
    if not found:
        await interaction.followup.send("No projects found in scan directories.")
        return

    existing = {p["cwd"] for p in db.list_projects()}
    new = [p for p in found if p["path"] not in existing]

    if not new:
        await interaction.followup.send(f"Found {len(found)} projects, all already registered.")
        return

    # Build select menu
    options = [
        discord.SelectOption(
            label=p["name"],
            description=f"{', '.join(p['markers'][:3])} | {p.get('description', '')[:80]}",
            value=p["name"],
        )
        for p in new[:25]
    ]

    view = discord.ui.View(timeout=120)
    select = discord.ui.Select(
        placeholder="Select projects to add...",
        options=options,
        min_values=1,
        max_values=len(options),
    )

    scan_results = {p["name"]: p for p in new}

    async def on_select(select_interaction: discord.Interaction):
        if not is_admin(select_interaction):
            await select_interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        added = []
        for name in select.values:
            p = scan_results.get(name)
            if p:
                db.add_project(name, p["path"], p["description"])
                added.append(name)
        await select_interaction.response.send_message(
            f"Added {len(added)} project(s): {', '.join(added)}"
        )
        view.stop()

    select.callback = on_select
    view.add_item(select)
    await interaction.followup.send(
        f"Found {len(new)} new project(s). Select to add:",
        view=view,
    )


@bot.tree.command(name="remove", description="Unregister a project")
@app_commands.describe(name="Project name")
async def cmd_remove(interaction: discord.Interaction, name: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Unauthorized.", ephemeral=True)
        return
    if db.remove_project(name):
        await interaction.response.send_message(f"Project **{name}** removed.")
    else:
        await interaction.response.send_message(f"Project '{name}' not found.")


@bot.tree.command(name="edit", description="Update a project's path or description")
@app_commands.describe(name="Project name", path="New path", description="New description")
async def cmd_edit(interaction: discord.Interaction, name: str, path: str = "", description: str = ""):
    if not is_admin(interaction):
        await interaction.response.send_message("Unauthorized.", ephemeral=True)
        return
    if not db.get_project(name):
        await interaction.response.send_message(f"Unknown project: {name}")
        return

    new_cwd = os.path.expanduser(path) if path else None
    new_desc = description if description else None

    if not new_cwd and not new_desc:
        await interaction.response.send_message("Provide `path` and/or `description` to update.")
        return

    if new_cwd and not os.path.isdir(new_cwd):
        await interaction.response.send_message(f"Directory not found: `{new_cwd}`")
        return

    db.update_project(name, cwd=new_cwd, description=new_desc)
    parts = []
    if new_cwd:
        parts.append(f"path → `{new_cwd}`")
    if new_desc:
        parts.append(f"desc → {new_desc}")
    await interaction.response.send_message(f"Project **{name}** updated: {', '.join(parts)}")


@bot.tree.command(name="ask", description="Send a prompt to a project")
@app_commands.describe(project="Project name", prompt="Your prompt")
async def cmd_ask(interaction: discord.Interaction, project: str, prompt: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Unauthorized.", ephemeral=True)
        return
    if not db.get_project(project):
        await interaction.response.send_message(f"Unknown project: {project}")
        return

    await interaction.response.send_message(f"Sending to **{project}**...")
    await _run_and_reply(interaction.channel, project, prompt)


@bot.tree.command(name="bind", description="Bind this channel/thread to a project")
@app_commands.describe(project="Project name")
async def cmd_bind(interaction: discord.Interaction, project: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Unauthorized.", ephemeral=True)
        return
    if not db.get_project(project):
        await interaction.response.send_message(f"Unknown project: {project}")
        return

    discord_id, scope = _get_binding_scope(interaction.channel)
    db.set_discord_binding(discord_id, scope, project_name=project)
    await interaction.response.send_message(
        f"Bound this {scope} to **{project}**. All messages here will route to it."
    )


@bot.tree.command(name="bind_guild", description="Bind this entire server to a project")
@app_commands.describe(project="Project name")
async def cmd_bind_guild(interaction: discord.Interaction, project: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Unauthorized.", ephemeral=True)
        return
    if not db.get_project(project):
        await interaction.response.send_message(f"Unknown project: {project}")
        return
    if not interaction.guild:
        await interaction.response.send_message("Not in a server.")
        return

    db.set_discord_binding(str(interaction.guild.id), "guild", project_name=project)
    await interaction.response.send_message(
        f"Bound this server to **{project}**. All channels inherit this unless overridden."
    )


@bot.tree.command(name="bind_category", description="Bind a category to a project")
@app_commands.describe(project="Project name")
async def cmd_bind_category(interaction: discord.Interaction, project: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Unauthorized.", ephemeral=True)
        return
    if not db.get_project(project):
        await interaction.response.send_message(f"Unknown project: {project}")
        return

    channel = interaction.channel
    category_id = None
    if isinstance(channel, discord.TextChannel) and channel.category_id:
        category_id = str(channel.category_id)
    elif isinstance(channel, discord.Thread) and channel.parent and channel.parent.category_id:
        category_id = str(channel.parent.category_id)

    if not category_id:
        await interaction.response.send_message("This channel is not in a category.")
        return

    db.set_discord_binding(category_id, "category", project_name=project)
    await interaction.response.send_message(
        f"Bound this category to **{project}**. All channels in it inherit this unless overridden."
    )


@bot.tree.command(name="unbind", description="Remove binding for this context")
async def cmd_unbind(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("Unauthorized.", ephemeral=True)
        return

    discord_id, scope = _get_binding_scope(interaction.channel)
    if db.remove_discord_binding(discord_id):
        await interaction.response.send_message(f"Binding removed for this {scope}.")
    else:
        await interaction.response.send_message("No binding found for this context.")


@bot.tree.command(name="bindings", description="Show all bindings in this server")
async def cmd_bindings(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("Unauthorized.", ephemeral=True)
        return

    all_bindings = db.list_discord_bindings()
    if not all_bindings:
        await interaction.response.send_message("No bindings configured. Use `/bind <project>`.")
        return

    lines = ["**Discord Bindings**\n"]
    for b in all_bindings:
        proj = b.get("project_name") or "(inherited)"
        sid = b.get("session_id")
        session_info = f" | session: `{sid[:12]}...`" if sid else ""
        lines.append(f"- `{b['discord_id']}` [{b['scope']}] → **{proj}**{session_info}")

    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="mcp", description="Manage MCP server access for a project")
@app_commands.describe(project="Project name")
async def cmd_mcp(interaction: discord.Interaction, project: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Unauthorized.", ephemeral=True)
        return
    proj = db.get_project(project)
    if not proj:
        await interaction.response.send_message(f"Unknown project: {project}")
        return

    available = get_mcp_servers_for_project(proj["cwd"])
    if not available:
        await interaction.response.send_message(f"No `.mcp.json` found for **{project}**.")
        return

    saved = db.get_project_mcp(project)
    lines = [f"**MCP servers for {project}:**\n"]
    for s in available:
        enabled = saved.get(s, True)
        status = "on" if enabled else "off"
        lines.append(f"- {s}: **{status}**")

    # Build toggle buttons
    view = discord.ui.View(timeout=120)
    for s in available[:20]:
        enabled = saved.get(s, True)
        label = f"Disable {s}" if enabled else f"Enable {s}"
        style = discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success

        button = discord.ui.Button(label=label, style=style, custom_id=f"mcp:{project}:{s}")

        def make_callback(server_name=s, proj_name=project):
            async def cb(inter: discord.Interaction):
                if not is_admin(inter):
                    await inter.response.send_message("Unauthorized.", ephemeral=True)
                    return
                current = db.get_project_mcp(proj_name)
                new_state = not current.get(server_name, True)
                db.set_project_mcp(proj_name, server_name, new_state)
                reset_memory(proj_name)
                state_label = "Enabled" if new_state else "Disabled"
                await inter.response.send_message(f"{state_label} **{server_name}** for {proj_name}.")
            return cb

        button.callback = make_callback(s, project)
        view.add_item(button)

    await interaction.response.send_message("\n".join(lines), view=view)


@bot.tree.command(name="status", description="Show session info for a project")
@app_commands.describe(project="Project name")
async def cmd_status(interaction: discord.Interaction, project: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Unauthorized.", ephemeral=True)
        return
    proj = db.get_project(project)
    if not proj:
        await interaction.response.send_message(f"Unknown project: {project}")
        return
    session_id = db.get_active_session(project)
    projects = db.list_projects()
    info = next((p for p in projects if p["name"] == project), None)
    running = "yes" if is_running(project) else "no"
    lines = [
        f"**Project:** {project}",
        f"**Path:** `{proj['cwd']}`",
        f"**Session:** `{session_id or 'none'}`",
        f"**Running:** {running}",
    ]
    if info and info.get("last_used"):
        lines.append(f"**Last used:** {info['last_used']}")
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="reset", description="Clear session for a project")
@app_commands.describe(project="Project name")
async def cmd_reset(interaction: discord.Interaction, project: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Unauthorized.", ephemeral=True)
        return
    if db.reset_session(project):
        reset_memory(project)
        # Also clear session from this binding
        discord_id, _ = _get_binding_scope(interaction.channel)
        binding = db.get_discord_binding(discord_id)
        if binding:
            db.update_discord_session(discord_id, "")
        await interaction.response.send_message(f"Session cleared for **{project}**.")
    else:
        await interaction.response.send_message(f"No active session for **{project}**.")


@bot.tree.command(name="history", description="Show past sessions for a project")
@app_commands.describe(project="Project name")
async def cmd_history(interaction: discord.Interaction, project: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Unauthorized.", ephemeral=True)
        return
    sessions = db.get_session_history(project)
    if not sessions:
        await interaction.response.send_message(f"No sessions for **{project}**.")
        return
    lines = []
    for s in sessions:
        active = " **(active)**" if s["active"] else ""
        lines.append(f"- `{s['session_id'][:12]}...`{active} — {s['created_at']}")
    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(name="model", description="Show or switch model")
@app_commands.describe(name="Model name (opus/sonnet/haiku)")
async def cmd_model(interaction: discord.Interaction, name: str = ""):
    if not is_admin(interaction):
        await interaction.response.send_message("Unauthorized.", ephemeral=True)
        return
    if not name:
        current = get_model()
        models = " | ".join(f"**{m}**" if m == current else m for m in AVAILABLE_MODELS)
        await interaction.response.send_message(f"Model: **{current}**\n\n{models}")
        return

    if set_model(name.lower()):
        await interaction.response.send_message(f"Model set to **{name.lower()}**.")
    else:
        await interaction.response.send_message(f"Unknown model. Available: {', '.join(AVAILABLE_MODELS)}")


@bot.tree.command(name="feedback", description="Leave feedback or manage it")
@app_commands.describe(text="Feedback text, or 'list', 'done <id>', 'rm <id>'")
async def cmd_feedback(interaction: discord.Interaction, text: str):
    if not is_admin(interaction):
        await interaction.response.send_message("Unauthorized.", ephemeral=True)
        return

    if text == "list":
        items = db.list_feedback()
        if not items:
            await interaction.response.send_message("No feedback yet.")
            return
        lines = []
        for f in items:
            mark = "[done]" if f["status"] == "done" else "[open]"
            lines.append(f"#{f['id']} {mark} {f['message']}")
        await interaction.response.send_message("\n".join(lines))
        return

    parts = text.split(maxsplit=1)
    if parts[0] in ("done", "rm") and len(parts) == 2:
        try:
            fid = int(parts[1])
        except ValueError:
            await interaction.response.send_message("Invalid ID.")
            return
        if parts[0] == "done":
            ok = db.resolve_feedback(fid)
        else:
            ok = db.delete_feedback(fid)
        label = "done" if parts[0] == "done" else "deleted"
        msg = f"#{fid} {label}." if ok else f"#{fid} not found."
        await interaction.response.send_message(msg)
        return

    fid = db.add_feedback(text)
    await interaction.response.send_message(f"Feedback #{fid} saved.")


# --- Text message handler (auto-routing) ---


_GREETINGS = frozenset([
    "hi", "hello", "hey", "yo", "sup", "hola",
    "ping", "test", "ok", "okay", "thanks", "thx", "bye",
])

_SWITCH_RE = re.compile(
    r"^(?:switch|change|use|go to)\s+(?:to\s+|project\s+)?(\S+)\s*$",
    re.IGNORECASE,
)


@bot.event
async def on_message(message: discord.Message):
    # Ignore own messages and bots
    if message.author == bot.user or message.author.bot:
        return

    # Ignore non-admin
    if message.author.id != ADMIN_DISCORD_ID:
        return

    # Ignore if it looks like a command prefix
    if message.content.startswith("!") or message.content.startswith("/"):
        return

    prompt = message.content.strip()
    if not prompt and not message.attachments:
        return

    # Handle attachments
    if message.attachments and not prompt:
        await _handle_attachments(message)
        return

    # Greeting shortcut
    word = prompt.lower().rstrip("!?.")
    if word in _GREETINGS:
        project_name, _ = _resolve_project(message.channel)
        if project_name:
            await message.reply(f"Ready — bound to **{project_name}**")
        else:
            projects = db.list_projects()
            if projects:
                await message.reply(f"Ready — {len(projects)} project(s). Use `/bind` to set one here.")
            else:
                await message.reply("Ready — no projects yet. Use `/add` or `/scan`.")
        return

    # Natural-language switch
    m = _SWITCH_RE.match(prompt)
    if m:
        target = m.group(1)
        projects = db.list_projects()
        names = [p["name"] for p in projects]
        matches = difflib.get_close_matches(target.lower(), [n.lower() for n in names], n=1, cutoff=0.6)
        if matches:
            matched_name = names[[n.lower() for n in names].index(matches[0])]
            discord_id, scope = _get_binding_scope(message.channel)
            db.set_discord_binding(discord_id, scope, project_name=matched_name)
            await message.reply(f"Bound to **{matched_name}**.")
        else:
            await message.reply(f"Project '{target}' not found. Use `/projects` to see registered projects.")
        return

    # Resolve project from hierarchy
    project_name, _ = _resolve_project(message.channel)

    # Auto-route if no binding
    if not project_name:
        projects = db.list_projects()
        if not projects:
            await message.reply("No projects registered. Use `/add` or `/scan`.")
            return

        matched = match_project_by_description(prompt, projects)
        if matched:
            project_name = matched
            logger.info("Auto-routed to %s", project_name)
        else:
            # Fall back to MRU
            active = [p for p in projects if p.get("session_id")]
            if active:
                active.sort(key=lambda p: p.get("last_used") or "", reverse=True)
                project_name = active[0]["name"]
            else:
                project_name = projects[0]["name"]

    # Handle attachments alongside text
    if message.attachments:
        for att in message.attachments:
            suffix = Path(att.filename).suffix or ""
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir="/tmp",
                                             prefix=f"dc_{Path(att.filename).stem}_") as tmp:
                tmp_path = tmp.name
            await att.save(tmp_path)
            prompt += f"\n[File attached: {tmp_path} (original: {att.filename})]"

    await _run_and_reply(message.channel, project_name, prompt)


async def _handle_attachments(message: discord.Message):
    """Handle messages that are just attachments with no text."""
    project_name, _ = _resolve_project(message.channel)
    if not project_name:
        projects = db.list_projects()
        if not projects:
            await message.reply("No projects registered.")
            return
        active = [p for p in projects if p.get("session_id")]
        if active:
            active.sort(key=lambda p: p.get("last_used") or "", reverse=True)
            project_name = active[0]["name"]
        else:
            project_name = projects[0]["name"]

    prompt_parts = []
    for att in message.attachments:
        suffix = Path(att.filename).suffix or ""
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, dir="/tmp",
                                         prefix=f"dc_{Path(att.filename).stem}_") as tmp:
            tmp_path = tmp.name
        await att.save(tmp_path)
        prompt_parts.append(f"[File attached: {tmp_path} (original: {att.filename})]")

    prompt = "\n".join(prompt_parts)
    if message.content:
        prompt = message.content + "\n" + prompt

    await _run_and_reply(message.channel, project_name, prompt)


# --- PID management ---


_PID_FILE = Path(__file__).parent / "data" / "discord_bot.pid"


def _acquire_pid() -> None:
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _PID_FILE.exists():
        pid = int(_PID_FILE.read_text().strip())
        try:
            os.kill(pid, 0)
            logger.error("Another Discord instance running (PID %d). Stop it first or delete %s", pid, _PID_FILE)
            sys.exit(1)
        except (ProcessLookupError, PermissionError):
            pass
    _PID_FILE.write_text(str(os.getpid()))


def _release_pid() -> None:
    try:
        _PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def main() -> None:
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN not set")
    if not ADMIN_DISCORD_ID:
        raise ValueError("ADMIN_DISCORD_ID not set")

    _acquire_pid()

    import atexit
    atexit.register(_release_pid)

    db.init_db()
    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
