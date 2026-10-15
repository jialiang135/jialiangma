import asyncio
import tomllib
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from api.main import app
from config.version import APP_VERSION


def test_runtime_version_matches_project_metadata_and_health_endpoint():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    async def get_health():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/api/health")

    response = asyncio.run(get_health())

    assert project["project"]["version"] == APP_VERSION
    assert app.version == APP_VERSION
    assert response.json() == {"status": "ok", "version": APP_VERSION}
