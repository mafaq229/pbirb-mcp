"""File-access sandbox, destructive-edit confirmation and audit log (issue #57).

Three policies, each read from the environment at call time:

``PBIRB_MCP_ALLOWED_ROOTS``
    ``os.pathsep``-separated directories (``:`` on POSIX, ``;`` on
    Windows). Every path argument (``path`` and every ``*_path``) must
    resolve — symlinks followed — inside one of them, or the call is
    refused with ``PathNotAllowed`` before the tool runs. ``*`` means
    unrestricted.

``PBIRB_MCP_REQUIRE_CONFIRM``
    Truthy/falsy. When on, a destructive tool (see
    ``pbirb_mcp.server.DESTRUCTIVE_TOOLS``) called without
    ``confirm=true`` doesn't run; the result says how to confirm.

``PBIRB_MCP_AUDIT_LOG``
    A file path. Every mutating call is appended to it as one JSON line:
    ``{ts, tool, path, args_sha256, ok, changed, error_type,
    in_transaction}``. Unset: no log.

When a variable is unset the *process default* applies. A plain in-process
``MCPServer`` (tests, the eval harness, library use — trusted calling code)
defaults to unrestricted roots and no confirmation; the ``pbirb-mcp``
console entry point, which an LLM client drives, calls
:func:`use_server_defaults` so the stdio server defaults to *the working
directory as the only root* and *confirmation on*.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "ALLOWED_ROOTS_ENV",
    "AUDIT_LOG_ENV",
    "REQUIRE_CONFIRM_ENV",
    "PathNotAllowed",
    "allowed_roots",
    "audit",
    "check_paths",
    "confirmation_payload",
    "confirmation_required",
    "expand_user_paths",
    "is_preview_path",
    "path_arguments",
    "preview_path",
    "request_fingerprint",
    "use_server_defaults",
]

ALLOWED_ROOTS_ENV = "PBIRB_MCP_ALLOWED_ROOTS"
REQUIRE_CONFIRM_ENV = "PBIRB_MCP_REQUIRE_CONFIRM"
AUDIT_LOG_ENV = "PBIRB_MCP_AUDIT_LOG"

# Process defaults for unset variables (see module docstring).
_default_roots: Optional[tuple[str, ...]] = None  # None = unrestricted
_default_require_confirm = False


def use_server_defaults(cwd: Optional[str] = None) -> None:
    """Apply the stdio-server defaults: root = ``cwd``, confirmation on."""
    global _default_roots, _default_require_confirm
    _default_roots = (str(Path(cwd or os.getcwd()).resolve()),)
    _default_require_confirm = True


def _reset_defaults_for_tests() -> None:
    global _default_roots, _default_require_confirm
    _default_roots = None
    _default_require_confirm = False


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


# ---- sandbox ---------------------------------------------------------------


class PathNotAllowed(PermissionError):
    """A path argument resolves outside every allowed root."""

    def __init__(self, param: str, path: str, resolved: str, roots: tuple[str, ...]):
        super().__init__(
            f"{param} {path!r} is outside the allowed roots ({', '.join(roots)}). "
            f"Use a file under one of them, or have the user widen {ALLOWED_ROOTS_ENV} "
            f"(directories separated by {os.pathsep!r}; '*' = unrestricted) in the "
            "MCP server configuration."
        )
        self.payload = {
            "param": param,
            "resolved_path": resolved,
            "allowed_roots": list(roots),
            "setting": ALLOWED_ROOTS_ENV,
        }


def allowed_roots() -> Optional[tuple[str, ...]]:
    """The resolved allowed roots, or ``None`` when unrestricted."""
    raw = os.environ.get(ALLOWED_ROOTS_ENV)
    if raw is None or not raw.strip():
        return _default_roots
    parts = [p.strip() for p in raw.split(os.pathsep) if p.strip()]
    if "*" in parts:
        return None
    return tuple(str(Path(os.path.expanduser(p)).resolve()) for p in parts)


# Files the server itself created for a preview (dry_run_edit's temp copy):
# allowed wherever they live, and their edits are not audited.
_PREVIEW_PATHS: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "pbirb_mcp_preview_paths", default=frozenset()
)


@contextlib.contextmanager
def preview_path(path: str) -> Iterator[None]:
    """Mark ``path`` as a server-made preview copy for the enclosed calls."""
    resolved = str(Path(path).resolve())
    token = _PREVIEW_PATHS.set(_PREVIEW_PATHS.get() | {resolved})
    try:
        yield
    finally:
        _PREVIEW_PATHS.reset(token)


def _is_preview(resolved: str) -> bool:
    return resolved in _PREVIEW_PATHS.get()


def is_preview_path(path: Any) -> bool:
    """Is ``path`` a server-made preview copy (see :func:`preview_path`)?"""
    if not isinstance(path, str) or not path or not _PREVIEW_PATHS.get():
        return False
    try:
        return _is_preview(str(Path(path).resolve()))
    except (OSError, RuntimeError, ValueError):
        return False


def path_arguments(arguments: dict[str, Any]) -> list[tuple[str, str]]:
    """``(name, value)`` for every file-path argument: ``path``, ``*_path``
    and ``*_dir`` (backup_report's ``dest_dir``)."""
    return [
        (k, v)
        for k, v in arguments.items()
        if (k == "path" or k.endswith(("_path", "_dir"))) and isinstance(v, str) and v
    ]


def expand_user_paths(arguments: dict[str, Any]) -> dict[str, Any]:
    """``arguments`` with a leading ``~`` / ``~user`` expanded in every path
    argument (issue #86). LLM clients send ``~/…`` routinely; the dispatcher
    runs this once, before :func:`check_paths`, so the sandbox check and the
    tool both see the expanded path. Returns a new dict only when needed."""
    expanded = {
        name: os.path.expanduser(value)
        for name, value in path_arguments(arguments)
        if value.startswith("~")
    }
    return {**arguments, **expanded} if expanded else arguments


def _inside(resolved: Path, root: str) -> bool:
    try:
        resolved.relative_to(root)
    except ValueError:
        return False
    return True


def check_paths(arguments: dict[str, Any]) -> None:
    """Raise :class:`PathNotAllowed` for the first path argument outside the roots."""
    roots = allowed_roots()
    if roots is None:
        return
    for name, value in path_arguments(arguments):
        # realpath semantics: `..` and symlinks are resolved, so neither
        # can step outside a root. `~` is expanded the same way the
        # dispatcher expands it for the tool (expand_user_paths).
        resolved = Path(os.path.expanduser(value)).resolve()
        if _is_preview(str(resolved)):
            continue
        if not any(_inside(resolved, root) for root in roots):
            raise PathNotAllowed(name, value, str(resolved), roots)


# ---- confirmation ------------------------------------------------------------


def confirmation_required() -> bool:
    raw = os.environ.get(REQUIRE_CONFIRM_ENV)
    if raw is None or not raw.strip():
        return _default_require_confirm
    return _truthy(raw)


def confirmation_payload(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    target = arguments.get("path") or arguments.get("target_path") or arguments.get("backup_path")
    return {
        "error_type": "ConfirmationRequired",
        "message": (
            f"{tool} is destructive and was not run. Confirm with the user, then call "
            f"it again with the same arguments plus confirm=true."
        ),
        "tool": tool,
        "path": target,
        "confirm_param": "confirm",
    }


def request_fingerprint(tool: str, arguments: dict[str, Any]) -> str:
    """Stable digest of a call (``confirm`` excluded) — binds a confirmation to it."""
    args = {k: v for k, v in arguments.items() if k not in ("confirm", "transaction_id")}
    blob = json.dumps({"tool": tool, "arguments": args}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---- audit log -------------------------------------------------------------------

_AUDIT_LOCK = threading.Lock()


def audit(
    tool: str,
    arguments: dict[str, Any],
    *,
    ok: bool,
    result: Any = None,
    error_type: Optional[str] = None,
    in_transaction: bool = False,
) -> None:
    """Append one JSON line for a mutating call when ``PBIRB_MCP_AUDIT_LOG`` is set."""
    log_path = os.environ.get(AUDIT_LOG_ENV, "").strip()
    if not log_path:
        return
    paths = {k: str(Path(v).resolve()) for k, v in path_arguments(arguments)}
    if any(_is_preview(p) for p in paths.values()):
        return  # a dry_run_edit preview copy, discarded afterwards
    if not paths and isinstance(result, dict) and isinstance(result.get("path"), str):
        paths = {"path": result["path"]}  # commit_editing_transaction
    changed = None
    if isinstance(result, dict):
        inner = result.get("result") if set(result) == {"result", "verify"} else result
        if isinstance(inner, dict) and "changed" in inner:
            changed = inner["changed"]
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool": tool,
        "path": paths.get("path") or next(iter(paths.values()), None),
        "paths": paths,
        "args_sha256": hashlib.sha256(
            json.dumps(arguments, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest(),
        "ok": ok,
        "changed": changed,
        "error_type": error_type,
        "in_transaction": in_transaction,
        "pid": os.getpid(),
    }
    line = json.dumps(entry, default=str) + "\n"
    with _AUDIT_LOCK:
        # O_APPEND: each line lands at the end even with several writers.
        fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
