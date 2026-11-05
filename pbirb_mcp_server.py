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
    PBIRB_MCP_TOOLSETS   comma-separated toolsets to enable (default: all);
                         --toolsets overrides it
    PBIRB_MCP_READ_ONLY  expose only read-only tools (default: off);
                         --read-only turns it on
"""

import argparse
import logging
import os
import sys
from typing import Optional

from pbirb_mcp.core import security, toolsets
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


def _parser() -> argparse.ArgumentParser:
    names = ", ".join(n for n in toolsets.TOOLSETS if n != toolsets.ALWAYS_ON)
    parser = argparse.ArgumentParser(
        prog="pbirb-mcp",
        description="MCP server (stdio) for editing Power BI Report Builder .rdl reports.",
    )
    parser.add_argument(
        "--toolsets",
        metavar="NAMES",
        help=(
            f"comma-separated toolsets to expose: {names}; or 'all' / 'default'. "
            f"'{toolsets.ALWAYS_ON}' is always on. Default: every toolset "
            f"(or ${toolsets.TOOLSETS_ENV})."
        ),
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help=f"expose only tools that change no file (or ${toolsets.READ_ONLY_ENV}=1).",
    )
    parser.add_argument(
        "--list-toolsets",
        action="store_true",
        help="print each toolset and its tools, then exit.",
    )
    return parser


def _list_toolsets() -> None:
    for name, tools in toolsets.TOOLSETS.items():
        note = " (always on)" if name == toolsets.ALWAYS_ON else ""
        if name in toolsets.OPT_IN_TOOLSETS:
            note = " (opt-in)"
        print(f"{name}{note}: {len(tools)} tools")
        print("  " + ", ".join(sorted(tools)))


def main(argv: Optional[list] = None) -> int:
    args = _parser().parse_args(argv)
    if args.list_toolsets:
        _list_toolsets()
        return 0
    try:
        # Flags override PBIRB_MCP_TOOLSETS / PBIRB_MCP_READ_ONLY (issue #56).
        server = MCPServer(toolsets=args.toolsets, read_only=True if args.read_only else None)
    except ValueError as exc:
        print(f"pbirb-mcp: {exc}", file=sys.stderr)
        return 2
    _configure_logging()
    logger = logging.getLogger("pbirb_mcp")
    logger.info("pbirb-mcp starting")
    # An LLM client drives this process: sandbox file access to the working
    # directory and require confirm=true for destructive edits unless the
    # environment says otherwise (issue #57).
    security.use_server_defaults()
    register_all_tools(server)
    server.run_stdio()
    return 0


if __name__ == "__main__":
    sys.exit(main())
