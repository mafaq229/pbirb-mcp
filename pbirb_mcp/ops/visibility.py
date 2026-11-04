"""Conditional visibility on any named ReportItem.

Group-level visibility belongs to ``set_group_visibility`` (commit 9) and
detail-row visibility to ``set_detail_row_visibility`` (commit 10) — those
edit ``<TablixMember>`` directly. This tool handles every other named
ReportItem: Tablix, Textbox, Image, Rectangle, Subreport, Chart, Line,
Map and GaugePanel (a gauge).

``<Visibility>`` is edited in place: ``<Hidden>`` is set, ``<ToggleItem>``
is set / kept / removed, and any other child survives. A new
``<Visibility>`` is inserted just before ``<Style>`` (which Report Builder
emits as the last child of ReportItem-derived elements), or appended when
``<Style>`` is absent.

Both inputs are validated before anything is written (issue #42), since
XSD and lint used to accept files Report Builder then rejects:

* ``Hidden`` must be a Boolean literal (``true`` / ``false``, any case)
  or an ``=`` expression with a non-empty, lexically sane body.
* ``ToggleItem`` must name an existing Textbox that is not the item (or
  inside it), sits in the same page region (Body / PageHeader /
  PageFooter), and — when it lives inside a tablix — the item lives
  inside that same tablix. :func:`toggle_item_problem` is shared with
  the ``dangling-toggle-item`` lint rule.
"""

from __future__ import annotations

from typing import Any, Optional

from lxml import etree

from pbirb_mcp.core.document import RDLDocument
from pbirb_mcp.core.encoding import encode_text
from pbirb_mcp.core.ids import (
    AmbiguousElementError,
    ElementNotFoundError,
)
from pbirb_mcp.core.xpath import RDL_NS, XPATH_NS, find_child, q
from pbirb_mcp.ops import vbexpr

# RDL ReportItem-derived types that legitimately accept <Visibility> as a
# child and that this tool is willing to target. Group / DataSet / Field /
# ReportParameter etc. also have a Name attribute but are out of scope.
_VISIBILITY_BEARING_TAGS = (
    "Tablix",
    "Textbox",
    "Image",
    "Rectangle",
    "Subreport",
    "Chart",
    "Line",
    "Map",
    "GaugePanel",
)

_REGION_TAGS = ("Body", "PageHeader", "PageFooter")


class _Unset:
    """Sentinel type: ``toggle_textbox`` was not passed (keep existing)."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


_UNSET: Any = _Unset()


def _resolve_named_report_item(doc: RDLDocument, name: str) -> etree._Element:
    # XPath constraint: tag local-name must be one of the visibility-bearing
    # ReportItem types. Avoids accidentally hitting <DataSet Name="...">,
    # <ReportParameter Name="...">, <Group Name="...">.
    type_clause = " or ".join(f"local-name()='{tag}'" for tag in _VISIBILITY_BEARING_TAGS)
    matches = list(
        doc.root.xpath(
            f".//*[@Name=$n and ({type_clause})]",
            namespaces=XPATH_NS,
            n=name,
        )
    )
    if not matches:
        raise ElementNotFoundError(
            f"no ReportItem named {name!r} (looked at: {', '.join(_VISIBILITY_BEARING_TAGS)})"
        )
    if len(matches) > 1:
        raise AmbiguousElementError(f"ReportItem name {name!r} matches {len(matches)} elements")
    return matches[0]


# ---- Hidden -------------------------------------------------------------


def normalise_hidden(value: Any) -> str:
    """Validate a ``<Hidden>`` value and return the text to write.

    Accepts ``true`` / ``false`` in any case (written lowercase) or an
    ``=`` expression whose body is non-empty, has no unterminated string
    literal and balanced delimiters. Raises ``ValueError`` otherwise —
    Report Builder rejects anything else at preview/publish.
    """
    if not isinstance(value, str):
        raise ValueError(f"hidden_expression must be a string; got {type(value).__name__}")
    text = value.strip()
    if text.lower() in ("true", "false"):
        return text.lower()
    if not text.startswith("="):
        raise ValueError(
            f"hidden_expression {value!r} is neither a Boolean literal ('true' / 'false') "
            "nor an '=' expression (e.g. '=Parameters!HideTable.Value')"
        )
    body = vbexpr.expression_body(text) or ""
    if not body.strip():
        raise ValueError("hidden_expression '=' has an empty expression body")
    bad_string = vbexpr.unterminated_string(body)
    if bad_string is not None:
        raise ValueError(f"hidden_expression {value!r} has an unterminated string literal")
    bad_delim = vbexpr.first_unbalanced_delimiter(body)
    if bad_delim is not None:
        raise ValueError(
            f"hidden_expression {value!r} has unbalanced {vbexpr.delimiter_family(bad_delim)} "
            f"(at {bad_delim.text!r})"
        )
    return encode_text(text)


# ---- ToggleItem ---------------------------------------------------------


def _region_of(el: etree._Element) -> Optional[str]:
    for anc in el.iterancestors():
        if not isinstance(anc.tag, str):
            continue
        local = etree.QName(anc).localname
        if local in _REGION_TAGS:
            return local
    return None


def _innermost_tablix(el: etree._Element) -> Optional[etree._Element]:
    for anc in el.iterancestors(f"{{{RDL_NS}}}Tablix"):
        return anc
    return None


def _is_ancestor(anc: etree._Element, el: etree._Element) -> bool:
    return any(a is anc for a in el.iterancestors())


def toggle_item_problem(
    root: etree._Element, owner: etree._Element, toggle_name: str
) -> Optional[str]:
    """Why ``toggle_name`` can't be the ``<ToggleItem>`` of ``owner``'s
    ``<Visibility>`` — or ``None`` when it can.

    ``owner`` is the element carrying the ``<Visibility>``: a ReportItem
    or a ``<TablixMember>`` (group visibility). Checks, in order: the
    name resolves to an element at all, that element is a Textbox, it is
    not the owner or inside it (ReportItem owners only — a member's
    cells aren't XML descendants of it), it is in the same page region,
    and a toggle inside a tablix only controls items inside that tablix.
    """
    matches = [
        el for el in root.iter() if isinstance(el.tag, str) and el.get("Name") == toggle_name
    ]
    items = [el for el in matches if etree.QName(el).localname == "Textbox"]
    if not items:
        if matches:
            kinds = sorted({etree.QName(el).localname for el in matches})
            return f"ToggleItem {toggle_name!r} names a {'/'.join(kinds)}, not a Textbox"
        return f"ToggleItem {toggle_name!r} does not name any textbox in the report"
    toggle = items[0]
    owner_local = etree.QName(owner).localname
    owner_label = f"{owner_local} {owner.get('Name')!r}" if owner.get("Name") else owner_local
    if owner_local != "TablixMember":
        if toggle is owner:
            return f"ToggleItem {toggle_name!r} is the item itself ({owner_label})"
        if _is_ancestor(owner, toggle):
            return (
                f"ToggleItem {toggle_name!r} is inside {owner_label}, which it would hide "
                "along with itself"
            )
    toggle_region = _region_of(toggle)
    owner_region = _region_of(owner)
    if toggle_region != owner_region:
        return (
            f"ToggleItem {toggle_name!r} is in {toggle_region or 'an unknown region'} but "
            f"{owner_label} is in {owner_region or 'an unknown region'}; a toggle must be "
            "in the same region"
        )
    tablix = _innermost_tablix(toggle)
    if tablix is not None and not _is_ancestor(tablix, owner):
        return (
            f"ToggleItem {toggle_name!r} is a cell of tablix {tablix.get('Name')!r}, but "
            f"{owner_label} is not inside that tablix; a toggle inside a tablix can only "
            "control items in the same or a nested scope"
        )
    return None


def textbox_names(root: etree._Element) -> set:
    """Every Textbox ``Name`` in the report."""
    return {el.get("Name") for el in root.iter(q("Textbox")) if el.get("Name")}


def guard_removed_toggle_targets(
    root: etree._Element, before: set, *, force: bool, what: str
) -> list:
    """Call after a removal has mutated the tree (before saving).

    ``before`` is :func:`textbox_names` taken before the removal. When a
    ``<ToggleItem>`` still points at a textbox that is now gone, raise
    ``ValueError`` (the caller hasn't saved, so the file is untouched) —
    unless ``force``, in which case those dangling ToggleItems are removed.
    Returns the names of the removed toggle targets (empty when none).
    """
    gone = before - textbox_names(root)
    if not gone:
        return []
    dangling = [t for t in root.iter(q("ToggleItem")) if (t.text or "").strip() in gone]
    if not dangling:
        return []
    targets = sorted({(t.text or "").strip() for t in dangling})
    if not force:
        raise ValueError(
            f"{what} would delete textbox(es) {targets} that a ToggleItem points at, "
            "leaving a dangling toggle Report Builder rejects. Re-point the toggle "
            "first, or pass force=true to remove those ToggleItems too."
        )
    for toggle in dangling:
        _remove_keep_layout(toggle)
    return targets


# ---- tool ---------------------------------------------------------------


def _remove_keep_layout(el: etree._Element) -> None:
    """Remove ``el``; when it is the last child, hand its tail (the
    parent's closing-tag indentation) to the previous sibling."""
    parent = el.getparent()
    prev = el.getprevious()
    if el.getnext() is None and prev is not None:
        prev.tail = el.tail
    parent.remove(el)


def set_element_visibility(
    path: str,
    element_name: str,
    hidden_expression: Optional[str] = None,
    toggle_textbox: Any = _UNSET,
    clear: bool = False,
) -> dict[str, Any]:
    """Set (or with ``clear=True`` remove) ``<Visibility>`` on a ReportItem.

    ``hidden_expression`` is required unless ``clear`` — ``true`` /
    ``false`` or an ``=`` expression. ``toggle_textbox``: omitted keeps
    the existing ``<ToggleItem>``; a name sets it (validated, see
    :func:`toggle_item_problem`); ``""`` or ``None`` removes it.

    Returns ``{element, kind, hidden_expression, toggle_textbox,
    changed}``; the file is saved only when ``changed``. Every validation
    error is raised before anything is written.
    """
    doc = RDLDocument.open(path)
    element = _resolve_named_report_item(doc, element_name)
    kind = etree.QName(element).localname
    existing = find_child(element, "Visibility")

    if clear:
        if hidden_expression is not None or toggle_textbox is not _UNSET:
            raise ValueError(
                "clear=true removes <Visibility>; don't also pass hidden_expression "
                "or toggle_textbox"
            )
        if existing is not None:
            _remove_keep_layout(existing)
            doc.save()
        return {
            "element": element_name,
            "kind": kind,
            "hidden_expression": None,
            "toggle_textbox": None,
            "changed": existing is not None,
        }

    if hidden_expression is None:
        raise ValueError("hidden_expression is required (or pass clear=true)")
    hidden_text = normalise_hidden(hidden_expression)

    if toggle_textbox is _UNSET:
        toggle_wanted: Any = _UNSET
    elif toggle_textbox is None or toggle_textbox == "":
        toggle_wanted = None
    elif not isinstance(toggle_textbox, str):
        raise ValueError(
            f"toggle_textbox must be a string or null; got {type(toggle_textbox).__name__}"
        )
    else:
        toggle_wanted = toggle_textbox.strip()
        problem = toggle_item_problem(doc.root, element, toggle_wanted)
        if problem is not None:
            raise ValueError(problem)

    changed = False
    vis = existing
    if vis is None:
        vis = etree.Element(q("Visibility"))
        style = find_child(element, "Style")
        if style is not None:
            style.addprevious(vis)
        else:
            element.append(vis)
        changed = True

    hidden = find_child(vis, "Hidden")
    if hidden is None:
        hidden = etree.Element(q("Hidden"))
        vis.insert(0, hidden)
        changed = True
    current = (hidden.text or "").strip()
    same = (
        current.lower() == hidden_text
        if hidden_text in ("true", "false")
        else current == hidden_text
    )
    if not same:
        hidden.text = hidden_text
        changed = True

    toggle = find_child(vis, "ToggleItem")
    if toggle_wanted is None:
        if toggle is not None:
            _remove_keep_layout(toggle)
            changed = True
    elif toggle_wanted is not _UNSET:
        if toggle is None:
            toggle = etree.Element(q("ToggleItem"))
            hidden.addnext(toggle)
            changed = True
        if toggle.text != toggle_wanted:
            toggle.text = toggle_wanted
            changed = True

    if changed:
        doc.save()
    final_toggle = find_child(vis, "ToggleItem")
    return {
        "element": element_name,
        "kind": kind,
        "hidden_expression": hidden.text,
        "toggle_textbox": final_toggle.text if final_toggle is not None else None,
        "changed": changed,
    }


__all__ = ["normalise_hidden", "set_element_visibility", "toggle_item_problem"]
