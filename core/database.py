"""
SQLite 数据库初始化与 CRUD 操作
"""
import datetime
import sqlite3
import os
from pathlib import Path
from contextlib import contextmanager
from typing import Optional
from loguru import logger

from config.settings import settings, PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "assets" / "personal_agent.db"


def get_db_path() -> str:
    return str(DB_PATH)


def init_database():
    """初始化数据库表结构"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    # 用户表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 兼容旧数据库：如果 role 列不存在则添加
    try:
        cursor.execute("SELECT role FROM users LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")
        logger.info("数据库迁移：users 表添加 role 列（旧用户默认 admin）")

    # 上传文件元数据表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL,
            file_size INTEGER DEFAULT 0,
            chunk_count INTEGER DEFAULT 0,
            file_hash TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_id) REFERENCES users(id)
        )
    """)
    # 兼容：旧数据库添加 file_hash 列
    try:
        cursor.execute("SELECT file_hash FROM files LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE files ADD COLUMN file_hash TEXT DEFAULT ''")
        logger.info("数据库迁移：files 表添加 file_hash 列")

    # 上传任务状态表（用于异步进度追踪）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS upload_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL UNIQUE,
            owner_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            file_size INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            progress INTEGER DEFAULT 0,
            chunk_count INTEGER DEFAULT 0,
            error TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 对话日志表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            agent_mode TEXT NOT NULL DEFAULT 'chat',
            conversation_id TEXT,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            reasoning TEXT,
            sources TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_id) REFERENCES users(id)
        )
    """)

    # Token 使用统计表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            cost_estimate REAL DEFAULT 0.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_id) REFERENCES users(id)
        )
    """)

    # 登录尝试记录（用于锁定）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            ip_address TEXT,
            success INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_user ON login_attempts(username, created_at)")

    conn.commit()
    conn.close()
    logger.info("数据库初始化完成: {}", str(DB_PATH))


@contextmanager
def get_db():
    """获取数据库连接的上下文管理器"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ========================================
# 用户操作
# ========================================

def create_admin_user(username: str, password_hash: str):
    """创建管理员用户（如果不存在）"""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
        if existing:
            # 确保已有管理员角色
            conn.execute(
                "UPDATE users SET role = 'admin' WHERE username = ? AND role != 'admin'",
                (username,),
            )
            conn.commit()
            logger.info("管理员账号已存在，跳过创建")
            return existing["id"]

        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
            (username, password_hash),
        )
        conn.commit()
        user_id = cursor.lastrowid
        logger.info("管理员账号创建成功: {} (id={}, role=admin)", username, user_id)
        return user_id


def create_user(username: str, password_hash: str, role: str = "user") -> int:
    """创建普通用户，返回用户ID。用户名已存在则抛异常"""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
        if existing:
            raise ValueError(f"用户名已被占用: {username}")

        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, password_hash, role),
        )
        conn.commit()
        user_id = cursor.lastrowid
        logger.info("用户创建成功: {} (id={}, role={})", username, user_id, role)
        return user_id


def get_user_by_username(username: str) -> Optional[dict]:
    """根据用户名查找用户"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    """根据ID查找用户"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None


# ========================================
# 文件元数据操作
# ========================================

def insert_file_record(owner_id: int, filename: str, filepath: str,
                       file_size: int = 0, chunk_count: int = 0) -> int:
    """插入文件记录，返回记录ID"""
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO files (owner_id, filename, filepath, file_size, chunk_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (owner_id, filename, filepath, file_size, chunk_count),
        )
        conn.commit()
        return cursor.lastrowid


def get_files_by_owner(owner_id: int) -> list[dict]:
    """获取某用户的所有文件"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM files WHERE owner_id = ? ORDER BY created_at DESC",
            (owner_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_file_by_id(file_id: int) -> Optional[dict]:
    """根据ID获取文件记录"""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        return dict(row) if row else None


def delete_file_record(file_id: int, owner_id: int) -> bool:
    """删除文件记录（需owner_id校验）"""
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM files WHERE id = ? AND owner_id = ?", (file_id, owner_id)
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_all_file_records(owner_id: int) -> int:
    """清空用户所有文件记录"""
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM files WHERE owner_id = ?", (owner_id,)
        )
        conn.commit()
        return cursor.rowcount


def update_file_chunk_count(file_id: int, chunk_count: int):
    """更新文件的chunk数量"""
    with get_db() as conn:
        conn.execute(
            "UPDATE files SET chunk_count = ? WHERE id = ?", (chunk_count, file_id)
        )
        conn.commit()


# ========================================
# 对话日志操作
# ========================================

def insert_chat_log(owner_id: int, agent_mode: str, question: str,
                    answer: str, reasoning: str = None, sources: str = None,
                    conversation_id: str = None) -> int:
    """插入对话日志"""
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO chat_logs (owner_id, agent_mode, conversation_id, question, "
            "answer, reasoning, sources) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (owner_id, agent_mode, conversation_id, question, answer, reasoning, sources),
        )
        conn.commit()
        return cursor.lastrowid


def get_chat_history(owner_id: int, limit: int = 50, offset: int = 0) -> list[dict]:
    """获取用户对话历史"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_logs WHERE owner_id = ? "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (owner_id, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def get_chat_history_count(owner_id: int) -> int:
    """获取对话历史总数"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM chat_logs WHERE owner_id = ?", (owner_id,)
        ).fetchone()
        return row["cnt"] if row else 0


def get_conversations(owner_id: int, limit: int = 30) -> list[dict]:
    """
    获取用户的对话组列表（按 conversation_id 分组）。
    旧数据（conversation_id 为 NULL）每条记录视为独立对话。
    返回按最新消息时间降序排列的对话摘要。
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                COALESCE(conversation_id, '__single_' || id) AS group_id,
                MIN(id) AS first_log_id,
                COUNT(*) AS turn_count,
                MAX(created_at) AS last_at,
                (SELECT question FROM chat_logs cl2
                 WHERE cl2.owner_id = ?
                   AND COALESCE(cl2.conversation_id, '__single_' || cl2.id) = COALESCE(cl.conversation_id, '__single_' || cl.id)
                 ORDER BY cl2.created_at ASC LIMIT 1
                ) AS first_question
            FROM chat_logs cl
            WHERE cl.owner_id = ?
            GROUP BY COALESCE(cl.conversation_id, '__single_' || cl.id)
            ORDER BY MAX(cl.created_at) DESC
            LIMIT ?
            """,
            (owner_id, owner_id, limit),
        ).fetchall()

        return [dict(r) for r in rows]


def get_chat_by_conversation_id(owner_id: int, conversation_id: str) -> list[dict]:
    """获取指定对话ID的所有记录，按时间升序"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_logs WHERE owner_id = ? AND conversation_id = ? "
            "ORDER BY created_at ASC",
            (owner_id, conversation_id),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_conversation(owner_id: int, group_id: str) -> int:
    """
    删除整个对话组的所有日志。
    - UUID 格式 → 删除 all rows with that conversation_id
    - __single_<id> 格式 → 只删除该 id 的单条记录
    返回删除条数。
    """
    with get_db() as conn:
        if group_id.startswith("__single_"):
            log_id = int(group_id.replace("__single_", ""))
            cursor = conn.execute(
                "DELETE FROM chat_logs WHERE id = ? AND owner_id = ?",
                (log_id, owner_id),
            )
        else:
            cursor = conn.execute(
                "DELETE FROM chat_logs WHERE conversation_id = ? AND owner_id = ?",
                (group_id, owner_id),
            )
        conn.commit()
        return cursor.rowcount


def _get_conn():
    """获取数据库连接（用于独立函数）"""
    import sqlite3
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def check_login_locked(username: str) -> bool:
    """检查用户是否因登录失败过多被锁定"""
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(minutes=settings.login_lockout_minutes)
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM login_attempts WHERE username = ? AND success = 0 AND created_at > ?",
        (username, cutoff.isoformat()),
    ).fetchone()
    conn.close()
    return (row["cnt"] if row else 0) >= settings.max_login_attempts


def record_login_attempt(username: str, ip_address: str = None, success: bool = False):
    """记录一次登录尝试"""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO login_attempts (username, ip_address, success) VALUES (?, ?, ?)",
        (username, ip_address, 1 if success else 0),
    )
    conn.commit()
    conn.close()


# ========================================
# 管理员：用户管理
# ========================================

def get_all_users() -> list[dict]:
    """获取所有用户列表（管理员用）"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, username, role, created_at FROM users ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def update_user_role(user_id: int, role: str) -> bool:
    """修改用户角色（管理员用）"""
    if role not in ("admin", "user"):
        raise ValueError("角色只能是 admin 或 user")
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE users SET role = ? WHERE id = ?", (role, user_id)
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_user_cascade(user_id: int) -> dict:
    """
    删除用户及其所有关联数据（管理员用）。
    返回各表删除条数。
    """
    counts = {}
    with get_db() as conn:
        for table in ("chat_logs", "token_usage", "login_attempts", "files", "upload_tasks"):
            cursor = conn.execute(f"DELETE FROM {table} WHERE owner_id = ?", (user_id,))
            counts[table] = cursor.rowcount

        # 审计日志按 user_id 字段
        cursor = conn.execute("DELETE FROM audit_log WHERE user_id = ?", (user_id,))
        counts["audit_log"] = cursor.rowcount

        cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        counts["users"] = cursor.rowcount

        conn.commit()
    return counts


# ========================================
# 管理员：全局数据查询
# ========================================

def get_all_chat_logs(limit: int = 50, offset: int = 0,
                      username: str = None) -> list[dict]:
    """跨用户查询对话日志（管理员用）"""
    with get_db() as conn:
        if username:
            rows = conn.execute(
                """SELECT cl.*, u.username FROM chat_logs cl
                   JOIN users u ON cl.owner_id = u.id
                   WHERE u.username = ?
                   ORDER BY cl.created_at DESC LIMIT ? OFFSET ?""",
                (username, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT cl.*, u.username FROM chat_logs cl
                   JOIN users u ON cl.owner_id = u.id
                   ORDER BY cl.created_at DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]


def get_all_files(limit: int = 50, offset: int = 0,
                  username: str = None) -> list[dict]:
    """跨用户查询文件列表（管理员用）"""
    with get_db() as conn:
        if username:
            rows = conn.execute(
                """SELECT f.*, u.username FROM files f
                   JOIN users u ON f.owner_id = u.id
                   WHERE u.username = ?
                   ORDER BY f.created_at DESC LIMIT ? OFFSET ?""",
                (username, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT f.*, u.username FROM files f
                   JOIN users u ON f.owner_id = u.id
                   ORDER BY f.created_at DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]


def get_global_stats() -> dict:
    """获取全局仪表盘数据（管理员用）"""
    import os
    with get_db() as conn:
        user_count = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()["cnt"]
        file_count = conn.execute("SELECT COUNT(*) as cnt FROM files").fetchone()["cnt"]
        today_chats = conn.execute(
            "SELECT COUNT(*) as cnt FROM chat_logs WHERE date(created_at) = date('now')"
        ).fetchone()["cnt"]
        total_chats = conn.execute("SELECT COUNT(*) as cnt FROM chat_logs").fetchone()["cnt"]
        total_tokens = conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) as cnt FROM token_usage"
        ).fetchone()["cnt"]
        total_cost = conn.execute(
            "SELECT COALESCE(SUM(cost_estimate), 0) as cnt FROM token_usage"
        ).fetchone()["cnt"]

    # 磁盘用量
    from config.settings import settings, PROJECT_ROOT
    upload_dir = settings.resolve_path(settings.upload_dir)
    disk_used_mb = 0
    if os.path.exists(upload_dir):
        for root, dirs, files in os.walk(upload_dir):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    disk_used_mb += os.path.getsize(fp) / (1024 * 1024)
                except OSError:
                    pass

    chroma_dir = settings.resolve_path(settings.chroma_persist_dir)
    chroma_mb = 0
    if os.path.exists(chroma_dir):
        for root, dirs, files in os.walk(chroma_dir):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    chroma_mb += os.path.getsize(fp) / (1024 * 1024)
                except OSError:
                    pass

    return {
        "user_count": user_count,
        "file_count": file_count,
        "today_chats": today_chats,
        "total_chats": total_chats,
        "total_tokens": total_tokens,
        "total_cost": round(total_cost, 6),
        "disk_used_mb": round(disk_used_mb, 2),
        "chroma_db_mb": round(chroma_mb, 2),
    }

