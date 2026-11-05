"""Report-cloning utility (Phase 6 commit 29).

``duplicate_report(src, dst, regenerate_ids=True)`` clones an ``.rdl``
file to a new path. The atomic writer behind ``RDLDocument.save_as``
applies (:func:`~pbirb_mcp.core.document.write_atomic`: temp file next to
the destination, rename on success).

When ``regenerate_ids=True`` (default), the helper rewrites every
``<rd:DataSourceID>`` and any ``<rd:ReportID>`` to a freshly-generated
``uuid4()`` so Power BI Report Builder doesn't refuse to load the
duplicate due to identity collision with the source. Some fixtures
also carry a ``<rd:ReportName>`` that we leave alone (it's a display
hint, not an identity field).

Refuses if ``dst_path`` already exists — no clobbering. The caller is
responsible for deleting the destination first if they really want to
overwrite (mirrors the safety story in ``add_data_source``,
``add_calculated_field``, etc.).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from pbirb_mcp.core.document import RDLDocument, write_atomic
from pbirb_mcp.core.xpath import RD_NS


def duplicate_report(
    src_path: str,
    dst_path: str,
    regenerate_ids: bool = True,
) -> dict[str, Any]:
    """Clone an ``.rdl`` to a new path.

    Atomic write convention: when ``regenerate_ids=True`` the helper
    opens the source, regenerates every ``<rd:DataSourceID>`` and
    optional ``<rd:ReportID>`` into a fresh ``uuid4()``, and saves to
    ``dst_path`` via the standard atomic ``save_as`` (temp file, rename).
    When ``regenerate_ids=False`` the file is copied byte-for-byte with
    the same atomic writer.

    Refuses if ``dst_path`` already exists. Refuses if ``src_path``
    isn't a regular file (or doesn't exist).

    Returns ``{src, dst, regenerated_ids: list[str]}`` listing the
    original GUIDs that were rewritten (empty list when
    ``regenerate_ids=False`` or the source had no rd: GUID children).
    """
    src = Path(src_path)
    dst = Path(dst_path)

    if not src.is_file():
        raise FileNotFoundError(f"src_path {src_path!r} is not a regular file")
    if dst.exists():
        raise FileExistsError(
            f"dst_path {dst_path!r} already exists; refuse to clobber. "
            "Delete the destination first if you really want to overwrite."
        )

    if not regenerate_ids:
        # Same atomic writer as save_as (temp file + rename, through a
        # symlink), so a failed copy never leaves a half-written dst
        # (issues #62, #64).
        write_atomic(dst, src.read_bytes())
        return {"src": str(src), "dst": str(dst), "regenerated_ids": []}

    # Open the source and work on a private copy: inside a transaction
    # open() hands back the transaction's live tree, and regenerating the
    # GUIDs in place would rewrite the SOURCE's ids at commit.
    doc = RDLDocument.open(src).copy_to(dst)
    regenerated: list[str] = []

    # rd:DataSourceID — one per <DataSource>.
    for ds_id in doc.root.iter(f"{{{RD_NS}}}DataSourceID"):
        if ds_id.text:
            regenerated.append(ds_id.text)
        ds_id.text = str(uuid.uuid4())

    # rd:ReportID — at most one per report; some fixtures don't have it.
    for report_id in doc.root.iter(f"{{{RD_NS}}}ReportID"):
        if report_id.text:
            regenerated.append(report_id.text)
        report_id.text = str(uuid.uuid4())

    # Save to the new path. RDLDocument.save_as is atomic (.tmp + rename).
    doc.save_as(dst)

    return {
        "src": str(src),
        "dst": str(dst),
        "regenerated_ids": regenerated,
    }


__all__ = ["duplicate_report"]
