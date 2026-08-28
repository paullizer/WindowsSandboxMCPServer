"""Package entry point: `python -m sandbox_mcp`."""

from __future__ import annotations

import asyncio
import json
import sys


def main() -> None:
    if "--selftest" in sys.argv:
        from .server import check_sandbox_setup

        print(json.dumps(asyncio.run(check_sandbox_setup()), indent=2))
        return

    from .server import server

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
