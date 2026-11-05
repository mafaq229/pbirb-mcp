#!/usr/bin/env python3
"""Entry point for the pbirb-mcp server.

Configures logging from environment variables and serves MCP over stdio via the
official MCP SDK (protocol revisions 2024-11-05 through 2026-07-28).

Environment variables:
    PBIRB_MCP_LOG_LEVEL  DEBUG|INFO|WARNING|ERROR (default: WARNING)
    PBIRB_MCP_LOG_FILE   path to log file (default: stderr)
    PBIRB_MCP_ALLOWED_ROOTS  directories (os.pathsep-separated) report paths
                         must be inside; '*' = unrestricted (default: the
                         working directory)
    PBIRB_MCP_REQUIRE_CONFIRM  destructive tools need confirm=true (default: on)
    PBIRB_MCP_AUDIT_LOG  append a JSON line per mutating call to this file
                         (default: off)
"""

import logging
import os
import sys

from pbirb_mcp.core import security
from pbirb_mcp.server import MCPServer
from pbirb_mcp.tools import register_all_tools


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
    # An LLM client drives this process: sandbox file access to the working
    # directory and require confirm=true for destructive edits unless the
    # environment says otherwise (issue #57).
    security.use_server_defaults()
    server = MCPServer()
    register_all_tools(server)
    server.run_stdio()
    return 0


if __name__ == "__main__":
    sys.exit(main())
