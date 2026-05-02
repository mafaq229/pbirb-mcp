"""Confirm the bundled RDL 2016/01 XSD is shipped + loads + accepts the
canonical clean fixture.

Drives off `pbirb_mcp/schemas/reportdefinition.xsd` — the file bundled in
v0.3.1 under the MS-RDL Open Specifications IP Rights Notice (see
`pbirb_mcp/schemas/NOTICE.md`). Closes the schema-conformance bug class
that escaped the v0.3.0 live-MCP sweep (four bugs only caught by Power
BI Report Builder load-test, not by the static gate).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from pbirb_mcp.core import schema as core_schema

FIXTURE_MIN = Path(__file__).parent / "fixtures" / "pbi_paginated_minimal.rdl"
SCHEMA_PATH = (
    Path(core_schema.__file__).resolve().parent.parent / "schemas" / "reportdefinition.xsd"
)
NOTICE_PATH = SCHEMA_PATH.parent / "NOTICE.md"


class TestBundledXsd:
    def test_xsd_file_is_shipped(self):
        """The XSD must be present at the package-relative path
        :func:`pbirb_mcp.core.schema._bundled_xsd_path` resolves to."""
        bundled = core_schema._bundled_xsd_path()
        assert bundled.is_file(), f"bundled XSD not found at {bundled}"
        assert bundled == SCHEMA_PATH

    def test_xsd_targets_2016_namespace(self):
        """The bundled XSD targets the 2016/01 namespace — anything else
        means we shipped the wrong version (the fixtures use 2016)."""
        tree = etree.parse(str(SCHEMA_PATH))
        target = tree.getroot().get("targetNamespace")
        assert target == "http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition"

    def test_xsd_available_returns_true_out_of_box(self):
        # Reset the module-level cache so this test sees the real lookup.
        core_schema._xsd_cache = None
        assert core_schema.xsd_available() is True

    def test_load_xsd_returns_compiled_schema(self):
        core_schema._xsd_cache = None
        schema = core_schema._load_xsd()
        assert isinstance(schema, etree.XMLSchema)

    def test_load_xsd_caches_schema(self):
        core_schema._xsd_cache = None
        first = core_schema._load_xsd()
        second = core_schema._load_xsd()
        assert first is second  # cached, not reparsed

    def test_clean_minimal_fixture_validates(self):
        """The canonical clean fixture must pass the bundled XSD —
        otherwise either the fixture or the schema is wrong."""
        core_schema._xsd_cache = None
        schema = core_schema._load_xsd()
        tree = etree.parse(str(FIXTURE_MIN))
        valid = schema.validate(tree)
        if not valid:
            errs = "\n".join(f"  L{e.line}: {e.message}" for e in list(schema.error_log)[:10])
            pytest.fail(f"clean fixture failed XSD validation:\n{errs}")

    def test_validate_against_xsd_passes_on_clean_fixture(self):
        core_schema._xsd_cache = None
        tree = etree.parse(str(FIXTURE_MIN))
        # Should not raise.
        core_schema.validate_against_xsd(tree)


class TestBundledNotice:
    def test_notice_file_is_shipped(self):
        """`NOTICE.md` documents the redistribution permission. Without
        it, downstream packagers can't tell why we ship Microsoft's
        copyrighted schema."""
        assert NOTICE_PATH.is_file(), f"NOTICE not found at {NOTICE_PATH}"

    def test_notice_cites_ip_rights_notice(self):
        text = NOTICE_PATH.read_text(encoding="utf-8")
        # The exact phrase from the MS-RDL IP Rights Notice that grants
        # redistribution. If this drifts, re-verify the source.
        assert "any schemas, IDLs, or code samples" in text
        assert "Microsoft" in text

    def test_notice_links_source(self):
        text = NOTICE_PATH.read_text(encoding="utf-8")
        assert "RdlMigration" in text  # source repo
