import os

from app.config import ensure_config, load_settings


def main() -> None:
    import uvicorn

    ensure_config()
    settings = load_settings()
    max_connections = max(64, min(4096, int(os.environ.get("DOLA_API_MAX_CONNECTIONS") or 512)))
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        limit_concurrency=max_connections,
        backlog=max_connections * 2,
        timeout_keep_alive=5,
    )


if __name__ == "__main__":
    main()
