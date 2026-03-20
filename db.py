import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "commander.db"


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript("""
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


def remove_project(name: str) -> bool:
    conn = get_conn()
    cursor = conn.execute("DELETE FROM projects WHERE name = ?", (name,))
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def list_projects() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT p.name, p.cwd, p.description,
               s.session_id, s.last_used
        FROM projects p
        LEFT JOIN sessions s ON s.project_name = p.name AND s.active = 1
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
