"""FastAPI host app for the token-obsession MCP server."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from token_obsession.core.config import get_settings
from token_obsession.mcp.server import mcp

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Start and stop the MCP session manager with the API app."""

    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)

app.mount(settings.mcp_mount_path, mcp.streamable_http_app())


@app.get("/health")
async def health() -> dict[str, str]:
    """Simple health endpoint."""

    return {"status": "ok", "chain": settings.default_chain, "mcp_path": settings.mcp_mount_path}
