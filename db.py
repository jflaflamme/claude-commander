import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "commander.db"


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript("""
        PRAGMA journal_mode = WAL;

        CREATE TABLE IF NOT EXISTS projects (
            name TEXT PRIMARY KEY,
            cwd TEXT NOT NULL,
            description TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL,
            session_id TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (project_name) REFERENCES projects(name)
        );

        CREATE TABLE IF NOT EXISTS permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(tool_name)
        );

        CREATE TABLE IF NOT EXISTS project_mcp (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL,
            server_name TEXT NOT NULL,
            allowed INTEGER DEFAULT 1,
            UNIQUE(project_name, server_name),
            FOREIGN KEY (project_name) REFERENCES projects(name)
        );

        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS discord_bindings (
            discord_id TEXT PRIMARY KEY,
            scope TEXT NOT NULL CHECK(scope IN ('guild', 'category', 'channel', 'thread')),
            project_name TEXT,
            session_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (project_name) REFERENCES projects(name)
        );
    """)
    conn.commit()
    conn.close()


def add_project(name: str, cwd: str, description: str = "") -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO projects (name, cwd, description) VALUES (?, ?, ?)",
        (name, cwd, description),
    )
    conn.commit()
    conn.close()


def update_project(
    name: str,
    cwd: str | None = None,
    description: str | None = None,
) -> bool:
    """Update path and/or description without resetting created_at."""
    if cwd is None and description is None:
        return False
    conn = get_conn()
    if cwd is not None and description is not None:
        cursor = conn.execute(
            "UPDATE projects SET cwd = ?, description = ? WHERE name = ?",
            (cwd, description, name),
        )
    elif cwd is not None:
        cursor = conn.execute(
            "UPDATE projects SET cwd = ? WHERE name = ?",
            (cwd, name),
        )
    else:
        cursor = conn.execute(
            "UPDATE projects SET description = ? WHERE name = ?",
            (description, name),
        )
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def remove_project(name: str) -> bool:
    conn = get_conn()
    # Clean up related records first (FK cascade not reliable on all SQLite builds)
    conn.execute("DELETE FROM sessions WHERE project_name = ?", (name,))
    conn.execute("DELETE FROM project_mcp WHERE project_name = ?", (name,))
    cursor = conn.execute("DELETE FROM projects WHERE name = ?", (name,))
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def list_projects() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT p.name, p.cwd, p.description,
               (SELECT session_id FROM sessions
                WHERE project_name = p.name AND active = 1
                ORDER BY last_used DESC LIMIT 1) AS session_id,
               (SELECT last_used FROM sessions
                WHERE project_name = p.name AND active = 1
                ORDER BY last_used DESC LIMIT 1) AS last_used
        FROM projects p
        ORDER BY p.name
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_project(name: str) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM projects WHERE name = ?", (name,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def save_session(project_name: str, session_id: str) -> None:
    conn = get_conn()
    # Deactivate old sessions for this project
    conn.execute(
        "UPDATE sessions SET active = 0 WHERE project_name = ? AND active = 1",
        (project_name,),
    )
    conn.execute(
        "INSERT INTO sessions (project_name, session_id) VALUES (?, ?)",
        (project_name, session_id),
    )
    conn.commit()
    conn.close()


def get_active_session(project_name: str) -> str | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE project_name = ? AND active = 1",
        (project_name,),
    ).fetchone()
    conn.close()
    return row["session_id"] if row else None


def touch_session(project_name: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE sessions SET last_used = CURRENT_TIMESTAMP "
        "WHERE project_name = ? AND active = 1",
        (project_name,),
    )
    conn.commit()
    conn.close()


def reset_session(project_name: str) -> bool:
    conn = get_conn()
    cursor = conn.execute(
        "UPDATE sessions SET active = 0 WHERE project_name = ? AND active = 1",
        (project_name,),
    )
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def get_session_history(project_name: str, limit: int = 10) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT session_id, active, created_at, last_used "
        "FROM sessions WHERE project_name = ? ORDER BY created_at DESC LIMIT ?",
        (project_name, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Permissions ---


def is_tool_allowed(tool_name: str) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM permissions WHERE tool_name = ?",
        (tool_name,),
    ).fetchone()
    conn.close()
    return row is not None


def allow_tool(tool_name: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO permissions (tool_name) VALUES (?)",
        (tool_name,),
    )
    conn.commit()
    conn.close()


def revoke_tool(tool_name: str) -> bool:
    conn = get_conn()
    cursor = conn.execute(
        "DELETE FROM permissions WHERE tool_name = ?",
        (tool_name,),
    )
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def list_allowed_tools() -> list[str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT tool_name FROM permissions ORDER BY tool_name"
    ).fetchall()
    conn.close()
    return [r["tool_name"] for r in rows]


# --- Project MCP ---


def set_project_mcp(
    project_name: str, server_name: str, allowed: bool
) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO project_mcp "
        "(project_name, server_name, allowed) VALUES (?, ?, ?)",
        (project_name, server_name, 1 if allowed else 0),
    )
    conn.commit()
    conn.close()


def get_project_mcp(project_name: str) -> dict[str, bool]:
    """Return {server_name: allowed} for a project."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT server_name, allowed FROM project_mcp "
        "WHERE project_name = ?",
        (project_name,),
    ).fetchall()
    conn.close()
    return {r["server_name"]: bool(r["allowed"]) for r in rows}


def get_allowed_mcp(project_name: str) -> list[str]:
    """Return list of allowed MCP server names."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT server_name FROM project_mcp "
        "WHERE project_name = ? AND allowed = 1",
        (project_name,),
    ).fetchall()
    conn.close()
    return [r["server_name"] for r in rows]


# --- Feedback ---


def add_feedback(message: str) -> int:
    conn = get_conn()
    cursor = conn.execute(
        "INSERT INTO feedback (message) VALUES (?)",
        (message,),
    )
    conn.commit()
    feedback_id = cursor.lastrowid
    conn.close()
    return feedback_id


def list_feedback(
    status: str | None = None,
) -> list[dict]:
    conn = get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM feedback WHERE status = ? "
            "ORDER BY created_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM feedback ORDER BY created_at DESC"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def resolve_feedback(feedback_id: int) -> bool:
    conn = get_conn()
    cursor = conn.execute(
        "UPDATE feedback SET status = 'done' WHERE id = ?",
        (feedback_id,),
    )
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def delete_feedback(feedback_id: int) -> bool:
    conn = get_conn()
    cursor = conn.execute(
        "DELETE FROM feedback WHERE id = ?",
        (feedback_id,),
    )
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


# --- Discord Bindings ---


def set_discord_binding(
    discord_id: str,
    scope: str,
    project_name: str | None = None,
    session_id: str | None = None,
) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO discord_bindings "
        "(discord_id, scope, project_name, session_id) VALUES (?, ?, ?, ?)",
        (discord_id, scope, project_name, session_id),
    )
    conn.commit()
    conn.close()


def get_discord_binding(discord_id: str) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM discord_bindings WHERE discord_id = ?",
        (discord_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_discord_session(discord_id: str, session_id: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE discord_bindings SET session_id = ? WHERE discord_id = ?",
        (session_id, discord_id),
    )
    conn.commit()
    conn.close()


def remove_discord_binding(discord_id: str) -> bool:
    conn = get_conn()
    cursor = conn.execute(
        "DELETE FROM discord_bindings WHERE discord_id = ?",
        (discord_id,),
    )
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def list_discord_bindings(guild_id: str | None = None) -> list[dict]:
    conn = get_conn()
    if guild_id:
        rows = conn.execute(
            "SELECT * FROM discord_bindings WHERE discord_id = ? "
            "OR discord_id LIKE ? ORDER BY scope",
            (guild_id, f"{guild_id}:%"),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM discord_bindings ORDER BY scope"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def resolve_discord_binding(
    thread_id: str | None,
    channel_id: str | None,
    category_id: str | None,
    guild_id: str | None,
) -> dict | None:
    """Walk up Discord hierarchy to find first binding."""
    conn = get_conn()
    for did in [thread_id, channel_id, category_id, guild_id]:
        if not did:
            continue
        row = conn.execute(
            "SELECT * FROM discord_bindings WHERE discord_id = ?",
            (did,),
        ).fetchone()
        if row:
            conn.close()
            return dict(row)
    conn.close()
    return None
