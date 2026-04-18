from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="flipper-mcp-bridge")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Run the HTTP REST API (for Home Assistant, curl, etc.) instead of MCP stdio.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP bind port (default: 8765)")
    args = parser.parse_args()

    if args.http:
        from .http_api import run_http
        run_http(host=args.host, port=args.port)
    else:
        from .server import main as run_stdio
        run_stdio()


if __name__ == "__main__":
    main()
