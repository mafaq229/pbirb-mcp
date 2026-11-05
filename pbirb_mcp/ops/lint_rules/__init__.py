"""Lint rules that live outside :mod:`pbirb_mcp.ops.lint`.

Each submodule of this package defines a module-level ``RULES`` dict
mapping rule name → ``rule(doc) -> list[Issue]`` (the same contract as the
``_rule_*`` functions in :mod:`pbirb_mcp.ops.lint`). Submodules are
discovered automatically — adding a rule family means adding a module
here, without touching the core registry. Rules are appended after the
core ones, in module-name order and then dict order.

Submodules may import shared helpers (``Issue``, ``_ancestor_chain``,
``_named_locator``, …) from :mod:`pbirb_mcp.ops.lint`; that module loads
this package only after its helpers are defined.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any, Callable


def load() -> dict[str, Callable[[Any], list]]:
    """Collect ``RULES`` from every submodule. Duplicate rule names raise."""
    rules: dict[str, Callable[[Any], list]] = {}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        for name, fn in getattr(module, "RULES", {}).items():
            if name in rules:
                raise ValueError(f"lint rule {name!r} defined twice (in {info.name})")
            rules[name] = fn
    return rules


__all__ = ["load"]
