"""Serve the dashboard: python -m carwatch.web

    python -m carwatch.web                    # http://127.0.0.1:8000
    python -m carwatch.web --port 8080
    python -m carwatch.web --reload           # for development
"""

from __future__ import annotations

import argparse
import sys

from carwatch.config import ConfigError, load_config
from carwatch.console import setup_console


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m carwatch.web", description="Run the CarWatch dashboard."
    )
    p.add_argument("-c", "--config", default="config.yaml")
    p.add_argument("--host", default="127.0.0.1", help="default: localhost only")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    args = p.parse_args(argv)
    setup_console()

    # Fail fast with a readable message rather than inside uvicorn's startup.
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    import uvicorn

    print(f"CarWatch dashboard  →  http://{args.host}:{args.port}")
    print(f"  config: {config.path}")
    print(f"  db    : {config.db_file}")

    if args.reload:
        # Reload mode needs an import string, not an app instance.
        uvicorn.run(
            "carwatch.web.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=True,
        )
    else:
        from carwatch.web.app import create_app

        uvicorn.run(create_app(args.config), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
