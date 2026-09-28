"""
工具注册中心
插件式工具注册，支持热加载和自动发现
使用 @register_tool 装饰器注册工具函数，无需修改 workflow 代码
"""

from collections.abc import Callable

from loguru import logger


class ToolRegistry:
    """全局工具注册中心单例"""

    def __init__(self):
        self._tools: dict[str, dict] = {}

    def register(
        self,
        name: str,
        description: str = "",
        category: str = "general",
        parameters: dict | None = None,
    ):
        """装饰器：注册一个工具函数"""

        def decorator(func: Callable) -> Callable:
            self._tools[name] = {
                "name": name,
                "func": func,
                "description": description or func.__doc__ or "",
                "category": category,
                "parameters": parameters or {},
            }
            logger.info(f"🔧 工具已注册: {name} (类别: {category})")
            return func

        return decorator

    def get_tool(self, name: str) -> Callable | None:
        """获取单个工具函数"""
        tool = self._tools.get(name)
        return tool["func"] if tool else None

    def get_tool_info(self, name: str) -> dict | None:
        """获取工具元信息"""
        return self._tools.get(name)

    def list_tools(self, category: str | None = None) -> list[dict]:
        """列出所有注册的工具（可按类别过滤）"""
        tools = list(self._tools.values())
        if category:
            tools = [t for t in tools if t["category"] == category]
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "category": t["category"],
                "parameters": t["parameters"],
            }
            for t in tools
        ]

    def list_categories(self) -> list[str]:
        """列出所有工具类别"""
        return sorted({t["category"] for t in self._tools.values()})

    def __len__(self) -> int:
        return len(self._tools)


# 全局单例
tool_registry = ToolRegistry()
register_tool = tool_registry.register
