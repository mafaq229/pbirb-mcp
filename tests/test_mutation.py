"""Change-aware writers in pbirb_mcp.core.mutation (#61)."""

from __future__ import annotations

from lxml import etree

from pbirb_mcp.core.mutation import (
    ChangeTracker,
    changed_by,
    fingerprint,
    put_child,
    remove_child_if_present,
    set_attr_if_different,
    set_node_text,
    set_text_if_different,
)
from pbirb_mcp.core.xpath import RDL_NS, q

ORDER = ("A", "B", "C")


def _parent(xml: str) -> etree._Element:
    return etree.fromstring(f'<P xmlns="{RDL_NS}">{xml}</P>')


def _locals(parent: etree._Element) -> list[str]:
    return [etree.QName(c).localname for c in parent]


class TestSetText:
    def test_same_value_is_no_change(self):
        p = _parent("<B>x</B>")
        assert set_text_if_different(p, "B", "x") is False

    def test_different_value_changes(self):
        p = _parent("<B>x</B>")
        assert set_text_if_different(p, "B", "y") is True
        assert p.find(q("B")).text == "y"

    def test_missing_child_is_created_in_order(self):
        p = _parent("<A>1</A><C>3</C>")
        assert set_text_if_different(p, "B", "2", order=ORDER) is True
        assert _locals(p) == ["A", "B", "C"]

    def test_missing_child_without_order_is_appended(self):
        p = _parent("<A>1</A>")
        assert set_text_if_different(p, "Z", "9") is True
        assert _locals(p) == ["A", "Z"]

    def test_set_node_text(self):
        node = _parent("<B>x</B>").find(q("B"))
        assert set_node_text(node, "x") is False
        assert set_node_text(node, "y") is True


class TestAttrAndRemove:
    def test_attr(self):
        p = _parent("")
        assert set_attr_if_different(p, "Name", "N") is True
        assert set_attr_if_different(p, "Name", "N") is False

    def test_remove(self):
        p = _parent("<A/>")
        assert remove_child_if_present(p, "A") is True
        assert remove_child_if_present(p, "A") is False


class TestFingerprint:
    def test_layout_whitespace_is_ignored(self):
        compact = _parent("<A><B>1</B></A>").find(q("A"))
        pretty = _parent("<A>\n  <B>1</B>\n</A>").find(q("A"))
        assert fingerprint(compact) == fingerprint(pretty)

    def test_leaf_whitespace_text_counts(self):
        space = _parent("<A> </A>").find(q("A"))
        empty = _parent("<A/>").find(q("A"))
        assert fingerprint(space) != fingerprint(empty)

    def test_empty_forms_are_equal(self):
        assert fingerprint(_parent("<A></A>").find(q("A"))) == fingerprint(
            _parent("<A/>").find(q("A"))
        )

    def test_attribute_order_is_ignored(self):
        a = _parent('<A x="1" y="2"/>').find(q("A"))
        b = _parent('<A y="2" x="1"/>').find(q("A"))
        assert fingerprint(a) == fingerprint(b)


class TestPutChild:
    @staticmethod
    def _new(local: str, text: str) -> etree._Element:
        node = etree.Element(q(local))
        node.text = text
        return node

    def test_identical_keeps_original_element(self):
        p = _parent("<A>1</A>\n<B>2</B>")
        original = p.find(q("B"))
        assert put_child(p, self._new("B", "2"), order=ORDER) is False
        assert p.find(q("B")) is original

    def test_different_replaces(self):
        p = _parent("<A>1</A><B>2</B>")
        assert put_child(p, self._new("B", "3"), order=ORDER) is True
        assert p.find(q("B")).text == "3"

    def test_missing_is_inserted_in_order(self):
        p = _parent("<A>1</A><C>3</C>")
        assert put_child(p, self._new("B", "2"), order=ORDER) is True
        assert _locals(p) == ["A", "B", "C"]


class TestTrackers:
    def test_changed_by(self):
        p = _parent("<A>1</A>")
        assert changed_by(p, set_text_if_different, p, "A", "1") is False
        assert changed_by(p, set_text_if_different, p, "A", "2") is True

    def test_change_tracker(self):
        p = _parent("<A>1</A>")
        with ChangeTracker(p) as t:
            p.find(q("A")).text = "1"
        assert t.changed is False
        with ChangeTracker(p) as t:
            p.find(q("A")).text = "2"
        assert t.changed is True
