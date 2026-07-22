"""
会话管理中心
Redis 持久化 + TTL 过期 + 上下文窗口自动裁剪
"""
import json
import time
from typing import Optional
from loguru import logger

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


class SessionManager:
    """
    会话管理器
    - 持久化会话到 Redis（重启不丢失）
    - 自动 TTL 过期（默认 24 小时无活动）
    - 上下文窗口自动裁剪（超过 max_tokens 自动摘要压缩）
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        session_ttl: int = 86400,      # 会话 TTL（秒），默认 24h
        max_context_tokens: int = 8000,  # 上下文窗口最大 token 数
        prefix: str = "session:",
    ):
        self.session_ttl = session_ttl
        self.max_context_tokens = max_context_tokens
        self.prefix = prefix
        self._redis = None
        self._fallback = {}  # 内存 fallback（Redis 不可用时）

        if REDIS_AVAILABLE:
            try:
                self._redis = redis.from_url(redis_url, decode_responses=True)
                self._redis.ping()
                logger.info("✅ 会话管理器已连接 Redis")
            except Exception as e:
                logger.warning(f"⚠️ Redis 连接失败，使用内存存储: {e}")
                self._redis = None

    # ─── 会话 CRUD ───

    def create_session(self, user_id: int, conversation_id: str, metadata: dict = None) -> str:
        """创建新会话"""
        key = f"{self.prefix}{user_id}:{conversation_id}"
        data = {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "created_at": time.time(),
            "last_active": time.time(),
            "messages": [],          # [{role, content, timestamp}]
            "total_tokens": 0,
            "metadata": metadata or {},
        }
        self._set(key, data)
        return conversation_id

    def add_message(self, user_id: int, conversation_id: str, role: str, content: str, tokens: int = 0):
        """向会话添加一条消息，自动触发上下文裁剪"""
        key = f"{self.prefix}{user_id}:{conversation_id}"
        data = self._get(key)
        if data is None:
            self.create_session(user_id, conversation_id)

        data = self._get(key)
        data["messages"].append({
            "role": role,
            "content": content,
            "timestamp": time.time(),
            "tokens": tokens,
        })
        data["total_tokens"] += tokens
        data["last_active"] = time.time()

        # 上下文窗口裁剪
        data = self._trim_context(data)

        self._set(key, data)

    def get_session(self, user_id: int, conversation_id: str) -> Optional[dict]:
        """获取会话完整数据"""
        key = f"{self.prefix}{user_id}:{conversation_id}"
        data = self._get(key)
        if data:
            data["last_active"] = time.time()
            self._set(key, data)
        return data

    def get_messages(self, user_id: int, conversation_id: str, limit: int = 50) -> list[dict]:
        """获取最近 N 条消息"""
        session = self.get_session(user_id, conversation_id)
        if not session:
            return []
        return session["messages"][-limit:]

    def delete_session(self, user_id: int, conversation_id: str):
        """删除会话"""
        key = f"{self.prefix}{user_id}:{conversation_id}"
        self._delete(key)

    def list_sessions(self, user_id: int) -> list[dict]:
        """列出用户的所有会话摘要"""
        pattern = f"{self.prefix}{user_id}:*"
        keys = self._keys(pattern)
        sessions = []
        for key in keys:
            data = self._get(key)
            if data:
                msgs = data.get("messages", [])
                sessions.append({
                    "conversation_id": data["conversation_id"],
                    "first_question": msgs[0]["content"][:100] if msgs else "",
                    "turn_count": len([m for m in msgs if m["role"] == "user"]),
                    "total_tokens": data.get("total_tokens", 0),
                    "last_active": data.get("last_active"),
                })
        sessions.sort(key=lambda s: s.get("last_active", 0), reverse=True)
        return sessions

    # ─── 上下文裁剪 ───

    def _trim_context(self, data: dict) -> dict:
        """当 token 超限时，自动裁剪最早的消息并生成摘要"""
        if data["total_tokens"] <= self.max_context_tokens:
            return data

        messages = data["messages"]
        trimmed = []
        removed_tokens = 0

        # 保留系统消息和最近的对话
        for msg in reversed(messages):
            if msg.get("role") == "system":
                trimmed.insert(0, msg)
                continue
            if data["total_tokens"] - removed_tokens > self.max_context_tokens * 0.7:
                removed_tokens += msg.get("tokens", 0)
                continue
            trimmed.insert(0, msg)

        if removed_tokens > 0:
            # 在顶部插入摘要消息
            summary = {
                "role": "system",
                "content": f"[上下文摘要] 更早的对话已自动裁剪（省去约 {removed_tokens} tokens）",
                "timestamp": time.time(),
                "tokens": 0,
            }
            trimmed.insert(0, summary)
            logger.info(f"📝 会话 {data['conversation_id']} 上下文裁剪: {removed_tokens} tokens")

        data["messages"] = trimmed
        data["total_tokens"] = data["total_tokens"] - removed_tokens
        return data

    # ─── Redis / 内存抽象层 ───

    def _set(self, key: str, data: dict):
        if self._redis:
            self._redis.setex(key, self.session_ttl, json.dumps(data, ensure_ascii=False))
        else:
            self._fallback[key] = data
            # 清理过期（简单实现）
            now = time.time()
            expired = [k for k, v in self._fallback.items()
                       if now - v.get("last_active", 0) > self.session_ttl]
            for k in expired:
                del self._fallback[k]

    def _get(self, key: str) -> Optional[dict]:
        if self._redis:
            raw = self._redis.get(key)
            return json.loads(raw) if raw else None
        data = self._fallback.get(key)
        if data and time.time() - data.get("last_active", 0) > self.session_ttl:
            del self._fallback[key]
            return None
        return data

    def _delete(self, key: str):
        if self._redis:
            self._redis.delete(key)
        else:
            self._fallback.pop(key, None)

    def _keys(self, pattern: str) -> list[str]:
        if self._redis:
            return list(self._redis.scan_iter(match=pattern))
        # 内存 fallback 不支持 pattern，手动过滤
        import fnmatch
        return [k for k in self._fallback if fnmatch.fnmatch(k, pattern)]


# 全局单例
session_manager = SessionManager()
