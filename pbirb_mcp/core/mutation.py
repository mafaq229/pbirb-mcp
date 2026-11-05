"""Change-aware tree writers (#61).

Mutating tools report ``changed`` so callers can decide whether to
verify or re-plan, so a writer must know whether it actually altered the
tree. These helpers write only when the new value differs and return
``True`` exactly when they did:

* :func:`set_text_if_different` — ``<parent><local>value</local></parent>``,
  creating the child (in XSD order when ``order`` is given) if absent;
* :func:`set_node_text` — the text of an element already in hand;
* :func:`put_child` — replace (or insert) a whole child element, keeping
  the original when the new one is identical;
* :func:`set_attr_if_different` — an attribute;
* :func:`remove_child_if_present` — drop a child;
* :func:`fingerprint` / :func:`changed_by` / :class:`ChangeTracker` — for
  writers that rebuild a whole subtree, compare its content (layout
  whitespace ignored) before and after.

:meth:`RDLDocument.save` independently skips the write when the tree is
unchanged, so a writer that over-reports never rewrites the file; these
helpers make the reported ``changed`` match.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.xpath import find_child, q


def _insert_in_order(parent: etree._Element, node: etree._Element, order: Sequence[str]) -> None:
    """Insert ``node`` before the first child that ``order`` places after
    it; append when there is none (or ``node`` isn't in ``order``)."""
    local = etree.QName(node).localname
    if local in order:
        idx = order.index(local)
        for i, child in enumerate(parent):
            if not isinstance(child.tag, str):
                continue
            child_local = etree.QName(child).localname
            if child_local in order and order.index(child_local) > idx:
                parent.insert(i, node)
                return
    parent.append(node)


def set_node_text(node: etree._Element, value: Optional[str]) -> bool:
    """Set ``node.text`` to ``value``; ``True`` iff it was different."""
    if node.text == value:
        return False
    node.text = value
    return True


def set_text_if_different(
    parent: etree._Element,
    local: str,
    value: str,
    *,
    order: Optional[Sequence[str]] = None,
) -> bool:
    """Make ``parent``'s ``<local>`` child hold ``value``.

    Creates the child when absent — inserted per ``order`` (the parent's
    XSD child sequence, by local name) or appended. Returns ``True`` iff
    the tree changed.
    """
    existing = find_child(parent, local)
    if existing is not None:
        return set_node_text(existing, value)
    node = etree.Element(q(local))
    node.text = value
    if order is None:
        parent.append(node)
    else:
        _insert_in_order(parent, node, order)
    return True


def set_attr_if_different(elem: etree._Element, name: str, value: str) -> bool:
    """Set attribute ``name``; ``True`` iff it was absent or different."""
    if elem.get(name) == value:
        return False
    elem.set(name, value)
    return True


def remove_child_if_present(parent: etree._Element, local: str) -> bool:
    """Remove ``parent``'s first ``<local>`` child; ``True`` iff one existed."""
    existing = find_child(parent, local)
    if existing is None:
        return False
    parent.remove(existing)
    return True


def _ws(text: Optional[str]) -> Optional[str]:
    return text if text and text.strip() else None


def fingerprint(elem: etree._Element) -> tuple:
    """A comparable value for ``elem``'s content: tags (namespace URIs, not
    prefixes), attributes (any order), leaf text and comments.

    Layout is ignored — whitespace-only text of elements that have
    children, and whitespace-only tails — because RDL has no mixed
    content: a subtree rebuilt compact and the pretty-printed one on disk
    compare equal. A leaf's text is compared exactly (``<Value> </Value>``
    is not ``<Value />``; ``<Value></Value>`` is). ``elem``'s own tail is
    not part of it.
    """
    if not isinstance(elem.tag, str):  # comment / processing instruction
        return ("#", str(elem.tag), elem.text)
    children = tuple(fingerprint(child) + (_ws(child.tail),) for child in elem)
    text = _ws(elem.text) if children else (elem.text or "")
    return (elem.tag, tuple(sorted(elem.attrib.items())), text, children)


def put_child(
    parent: etree._Element,
    new_child: etree._Element,
    *,
    order: Optional[Sequence[str]] = None,
) -> bool:
    """Replace ``parent``'s child with ``new_child``'s tag by ``new_child``,
    or insert it (per ``order``) when there is none.

    Returns ``True`` iff the tree changed. When the existing child has the
    same :func:`fingerprint` as ``new_child`` the original element stays
    (its layout included) and ``False`` is returned.
    """
    existing = parent.find(new_child.tag)
    if existing is None:
        if order is None:
            parent.append(new_child)
        else:
            _insert_in_order(parent, new_child, order)
        return True
    if fingerprint(existing) == fingerprint(new_child):
        return False
    parent.replace(existing, new_child)
    return True


def changed_by(elem: etree._Element, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
    """Run ``fn(*args, **kwargs)``; ``True`` iff ``elem``'s subtree changed."""
    before = fingerprint(elem)
    fn(*args, **kwargs)
    return fingerprint(elem) != before


class ChangeTracker:
    """Did a subtree change? ``with ChangeTracker(elem) as t: ...; t.changed``.

    For writers that remove and rebuild children wholesale (sort
    expressions, available values, …), where tracking each write is
    impractical. ``elem`` must stay in the tree (compare an ancestor that
    isn't replaced).
    """

    def __init__(self, elem: etree._Element) -> None:
        self.elem = elem
        self._before = fingerprint(elem)
        self.changed = False

    def __enter__(self) -> ChangeTracker:
        return self

    def __exit__(self, *exc: object) -> None:
        self.changed = fingerprint(self.elem) != self._before

    def check(self) -> bool:
        """The current answer (usable inside the ``with`` block)."""
        self.changed = fingerprint(self.elem) != self._before
        return self.changed


__all__ = [
    "ChangeTracker",
    "changed_by",
    "fingerprint",
    "put_child",
    "remove_child_if_present",
    "set_attr_if_different",
    "set_node_text",
    "set_text_if_different",
]
