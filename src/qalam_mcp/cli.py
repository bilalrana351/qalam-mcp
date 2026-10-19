import argparse
import asyncio
import getpass
import json
import os
import secrets
import sys
from pathlib import Path

from .client import QalamClient
from .errors import QalamError
from .session import SessionStore


def configuration() -> dict:
    # The venv executable makes this independent of an agent's working directory and PATH.
    executable = Path(sys.executable).parent / ("qalam-mcp.exe" if os.name == "nt" else "qalam-mcp")
    return {"mcpServers": {"qalam": {"command": str(executable), "args": ["serve"]}}}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Qalam attendance and grades for any MCP agent.")
    sub = root.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="Start the MCP server (stdio by default).")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    login = sub.add_parser("login", help="Sign in once; password prompt is hidden.")
    login.add_argument("--username")
    login.add_argument(
        "--browser", action="store_true", help="Sign in in a visible Chromium window."
    )
    login.add_argument(
        "--cdp-url", help="Use an existing Chromium debug endpoint for browser login."
    )
    login.add_argument("--timeout", type=int, default=300)
    status = sub.add_parser("status", help="Check session validity.")
    status.add_argument("--offline", action="store_true")
    sub.add_parser("logout", help="Forget shared local session.")
    sub.add_parser("config", help="Print local MCP JSON configuration; contains no credentials.")
    token = sub.add_parser(
        "token", help="Create/show the separate secret used for HTTP MCP access."
    )
    token.add_argument("--rotate", action="store_true")
    return root


def main() -> None:
    args = parser().parse_args()
    store = SessionStore()
    try:
        if args.command in (None, "serve"):
            from .server import BearerAuth, create_server

            def csv_env(name):
                return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()] or None

            server = create_server(
                allowed_hosts=csv_env("QALAM_ALLOWED_HOSTS"),
                allowed_origins=csv_env("QALAM_ALLOWED_ORIGINS"),
            )
            if getattr(args, "transport", "stdio") == "stdio":
                server.run(transport="stdio")
            else:
                import uvicorn

                token = os.environ.get("QALAM_MCP_TOKEN", "").strip()
                path = store.directory / "access-token"
                if not token and path.exists():
                    token = path.read_text().strip()
                if len(token) < 32:
                    raise QalamError(
                        "TOKEN_REQUIRED",
                        "Run qalam-mcp token first, or set "
                        "QALAM_MCP_TOKEN to a secret of at least 32 characters.",
                    )
                app = BearerAuth(server.streamable_http_app(), token)
                uvicorn.run(
                    app, host=args.host, port=args.port, log_level="warning", access_log=False
                )
            return
        if args.command == "login":
            if args.browser or args.cdp_url:
                from .browser_auth import browser_login

                print(
                    "Complete Qalam sign-in in the browser. Waiting for your student portal…",
                    file=sys.stderr,
                )
                result = asyncio.run(browser_login(store, args.timeout, args.cdp_url))
            else:
                username = args.username or input("Qalam username: ")
                password = getpass.getpass("Qalam password: ")
                result = QalamClient(store).login(username, password)
                password = None
        elif args.command == "status":
            result = QalamClient(store).status(not args.offline)
        elif args.command == "logout":
            store.clear()
            result = {"ok": True, "message": "Shared local session removed."}
        elif args.command == "config":
            result = configuration()
            if "QALAM_STATE_DIR" in os.environ:
                result["mcpServers"]["qalam"]["env"] = {"QALAM_STATE_DIR": str(store.directory)}
        elif args.command == "token":
            with store.locked():
                path = store.directory / "access-token"
                if args.rotate or not path.exists():
                    store.write_private("access-token", secrets.token_urlsafe(48))
                print(path.read_text().strip())
            return
        else:
            return
        print(json.dumps(result, indent=2))
    except QalamError as exc:
        print(json.dumps(exc.as_dict()), file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("Cancelled.", file=sys.stderr)
        sys.exit(130)
    except OSError:
        print(
            "Could not access Qalam's local state directory. Check QALAM_STATE_DIR permissions.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
