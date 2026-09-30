"""
core/db/models.py —— 全部 SQLAlchemy 模型（9 张表）。只依赖 base。
================================================

由 core/database.py 拆分而来（2026-09；拆分前是单文件 1366 行 / 57 个函数）。
数据库层只有 `core.db` 这一个入口，没有兼容门面 —— 直接从这里导入。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Float,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import (  # 拆分前同处一个命名空间，跨模块引用要显式导入
    Base,
    UTCDateTime,
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'user'"))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class FileRecord(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    filepath: Mapped[str] = mapped_column(String, nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    chunk_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    file_hash: Mapped[str] = mapped_column(String, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class UploadTask(Base):
    __tablename__ = "upload_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'pending'"))
    progress: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    chunk_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error: Mapped[str] = mapped_column(Text, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class ChatLog(Base):
    __tablename__ = "chat_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_mode: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'chat'"))
    conversation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    # reasoning / sources 都是 JSON 字符串塞在 TEXT 里。
    # 刻意**不**改成 JSON 列：改了存量数据读不出来。
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    sources: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class TokenUsage(Base):
    __tablename__ = "token_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    reasoning_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cached_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cost_estimate: Mapped[float] = mapped_column(Float, server_default=text("0.0"))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String, nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String, nullable=True)
    success: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class AuditLog(Base):
    """
    审计日志。

    原先建表语句写在 core/audit.py 里，导致 schema 分散在两个模块。
    这里收编，让"表结构的唯一来源"是这个文件。
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    resource: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String, server_default=text("'success'"))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP")
    )


class EvalReport(Base):
    """
    评测报告。

    这张表**原本是孤儿**：库里有、但全仓库没有任何代码创建或读写它
    （eval 功能被删除了一半，schema 留了下来）。这里接着它原来的字段设计
    补全实现，并加上异步任务需要的状态字段。

    指标分两类：
    - RAGAS（faithfulness / answer_relevancy / context_precision / context_recall）
      存 metrics_json
    - 自建的"诚实度"——知识库外问题是否如实说不知道，存 honesty_rate
    """

    __tablename__ = "eval_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    testset_name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, server_default=text("'pending'"))
    progress: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    total_questions: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    completed: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    answered_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    accuracy: Mapped[float] = mapped_column(Float, server_default=text("0.0"))
    hallucination_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    hallucination_rate: Mapped[float] = mapped_column(Float, server_default=text("0.0"))
    honesty_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    poor_retrieval_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    results_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommendations_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 本次评测真正生效的配置快照（JSON-in-TEXT，与 metrics_json 同款做法）。
    # **为什么必须有它**：原先报告只存结果、不存"这次用的是哪套检索参数"，
    # 跑两次得到的两个分数于是无法解释差异 —— 只能改 .env、重跑、人肉对照，
    # 评测因此"证明不了任何事"。落一份配置快照后，A/B 两次的差异字段可以
    # 直接 diff 出来（见 ``api/routes/eval_routes.py`` 的 compare 接口）。
    #
    # 加列不需要数据库迁移：新库由 Base.metadata.create_all 建好，旧库由
    # core/database.py 的 _LEGACY_COLUMNS 在 init_database 时 ALTER TABLE 补齐。
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str] = mapped_column(Text, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=text("CURRENT_TIMESTAMP")
    )


# ============================================================
# 引擎与会话
# ============================================================
