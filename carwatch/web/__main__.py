"""Serve the dashboard: python -m carwatch.web

    python -m carwatch.web                    # http://127.0.0.1:8000
    python -m carwatch.web --port 8080
    python -m carwatch.web --reload           # for development

The desktop icon runs it with no window, stopping on its own once no page has
been open for five minutes (see carwatch.web.idle), and writing to a log file
in place of the window it does not have:

    pythonw -m carwatch.web --port 8009 --idle-exit 5 --log logs/dashboard.log
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

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
    p.add_argument(
        "--idle-exit",
        type=float,
        default=0,
        metavar="MINUTES",
        help="stop once no page has been open this long (default: never)",
    )
    p.add_argument("--log", metavar="FILE", help="write output here instead of the console")
    args = p.parse_args(argv)
    _redirect_output(args.log)
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

        app = create_app(args.config)
        server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port))
        if args.idle_exit > 0:
            from carwatch.web.idle import IdleWatch

            app.state.idle = IdleWatch(
                args.idle_exit * 60, is_busy=app.state.jobs.is_busy
            )

            def stop() -> None:
                print(f"No page open for {args.idle_exit:g} minutes; stopping.", flush=True)
                server.should_exit = True

            app.state.idle.watch(stop)
            print(f"  stops after {args.idle_exit:g} minutes with no page open")
        server.run()
    return 0


def _redirect_output(log: str | None) -> None:
    """Send output to `log`, or nowhere when there is no console to send it to.

    Under pythonw (the desktop icon) there is no console: stdout and stderr are
    None, and the first thing to print — uvicorn's startup line — would fail.
    The file is started afresh each time, so it holds this session only.
    """
    if log:
        path = Path(log)
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(path, "w", encoding="utf-8", errors="replace", buffering=1)
    elif sys.stdout is None or sys.stderr is None:
        stream = open(os.devnull, "w", encoding="utf-8")
    else:
        return
    sys.stdout = sys.stderr = stream


if __name__ == "__main__":
    raise SystemExit(main())
