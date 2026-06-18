"""CLI entrypoint for local development."""

import uvicorn

from token_obsession.core.config import get_settings


def main() -> None:
    """Run the FastAPI app with uvicorn."""

    settings = get_settings()
    uvicorn.run(
        "token_obsession.api.app:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
    )


if __name__ == "__main__":
    main()
