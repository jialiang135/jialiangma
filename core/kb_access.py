"""
知识库访问控制
==============

**唯一**回答"这次请求该查谁的知识库、能不能查"的地方。

背景：这是一次真实越权事故的修复。原实现把 `owner_id` 写死成 `1`：

```python
# agent/chat_agent.py（修复前）
KB_OWNER_ID = 1                      # 工具里写死
...retrieve(query=..., owner_id=1)   # 检索里写死
```

而 `POST /api/chat/stream/public` 的 docstring 明明写着
"无需登录，**但未登录用户无知识库访问权限**"、传下去的 `owner_id=0`——
这个防护**被下层单方面无视了**。实测：不带任何 token 请求该端点，
问"手机号和邮箱是多少"，直接拿到了完整的个人信息。

这类洞比"忘了鉴权"更危险：**上层读起来是防住的**（`owner_id=0` 看着人畜无害），
只有顺着调用链往下读两层才会发现被覆盖了。所以规则现在收在这一个函数里，
任何要检索知识库的地方都必须过它，不再各自写死。

产品语义
--------
- 这是"单管理员的个人数字分身"，知识库属于管理员
- **已登录**用户（面试官注册的账号）共享这份知识库 —— 这是产品本意
- **匿名**默认拿不到（`allow_anonymous_kb_access=False`）
"""

from __future__ import annotations

from config.settings import settings


def resolve_kb_owner(request_owner_id: int | None) -> int | None:
    """
    决定这次请求该查谁的知识库。

    Args:
        request_owner_id: 请求方的 owner_id。已登录是真实用户 ID，
                          匿名是 0 或 None。

    Returns:
        int  —— 可以检索，返回知识库所有者的 owner_id
        None —— 不允许检索知识库（匿名且未开放匿名访问）
    """
    if request_owner_id:
        return settings.shared_kb_owner_id
    if settings.allow_anonymous_kb_access:
        return settings.shared_kb_owner_id
    return None


def can_search_kb(request_owner_id: int | None) -> bool:
    """便捷判断：这次请求能不能检索知识库。"""
    return resolve_kb_owner(request_owner_id) is not None
