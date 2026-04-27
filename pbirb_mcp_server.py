#!/usr/bin/env python3
"""Entry point for the pbirb-mcp server.

Configures logging from environment variables and starts the JSON-RPC stdio loop.

Environment variables:
    PBIRB_MCP_LOG_LEVEL  DEBUG|INFO|WARNING|ERROR (default: WARNING)
    PBIRB_MCP_LOG_FILE   path to log file (default: stderr)
"""

import logging
import os
import sys

from pbirb_mcp.server import MCPServer


def _configure_logging() -> None:
    level_name = os.environ.get("PBIRB_MCP_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    log_file = os.environ.get("PBIRB_MCP_LOG_FILE")

    handlers: list[logging.Handler] = []
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    else:
        handlers.append(logging.StreamHandler(sys.stderr))

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def main() -> int:
    _configure_logging()
    logger = logging.getLogger("pbirb_mcp")
    logger.info("pbirb-mcp starting")
    server = MCPServer()
    server.run_stdio()
    return 0


if __name__ == "__main__":
    sys.exit(main())
