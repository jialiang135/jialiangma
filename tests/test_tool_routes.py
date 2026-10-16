"""Tool registry endpoints return a coherent listing and category view."""

from tests.conftest import run_async


def test_tool_routes_use_registry_listing_and_count(monkeypatch):
    import api.routes.tool_routes as routes

    class Registry:
        def __len__(self):
            return 2

        def list_tools(self):
            return [{"name": "search"}, {"name": "summary"}]

        def list_categories(self):
            return ["knowledge", "management"]

    monkeypatch.setattr(routes, "tool_registry", Registry())
    user = {"owner_id": 1}
    assert run_async(routes.list_tools(user)) == {
        "success": True,
        "tools": [{"name": "search"}, {"name": "summary"}],
        "categories": ["knowledge", "management"],
        "total": 2,
    }
    assert run_async(routes.list_categories(user)) == {
        "success": True,
        "categories": ["knowledge", "management"],
    }
