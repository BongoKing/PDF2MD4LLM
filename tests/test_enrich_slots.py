"""Slot-based rebuild: failed/filtered/unchosen slots restore the original
placeholder, never a dangling temp-dir image ref. This is the load-bearing
regression test for the bug that prompted the refactor.
"""
import re
from pathlib import Path

import pdf2md


def test_rebuild_md_replaces_each_ref_in_order():
    """Two refs with identical paths still substitute independently and in
    document order."""
    md_with_refs = (
        "before ![](img/a.png) middle ![](img/a.png) end"
    )
    refs = list(pdf2md.IMAGE_REF_RE.finditer(md_with_refs))
    assert len(refs) == 2
    out = pdf2md._rebuild_md(md_with_refs, refs, ["FIRST", "SECOND"])
    assert out == "before FIRST middle SECOND end"


def test_rebuild_handles_failure_then_success():
    """A failed image keeps its placeholder; the next succeeds. The classic
    pre-fix bug was that the failure-slot held a dead temp-dir ref."""
    md_with_refs = "x ![](tmp/a.png) y ![](tmp/b.png) z"
    refs = list(pdf2md.IMAGE_REF_RE.finditer(md_with_refs))
    placeholder = "**==> picture [42 x 42] intentionally omitted <==**"

    # Slot 0 failed -> restore placeholder. Slot 1 succeeded -> Claude text.
    slot_text = [placeholder, "## ENRICHED"]
    out = pdf2md._rebuild_md(md_with_refs, refs, slot_text)

    assert "tmp/a.png" not in out                    # dead ref gone
    assert "## ENRICHED" in out                # success preserved
    assert placeholder in out                        # placeholder restored
    # Re-run via PLACEHOLDER_RE picks up the failed slot for retry.
    assert pdf2md.PLACEHOLDER_RE.search(out) is not None


def test_rebuild_handles_quota_pattern():
    """Quota fires mid-loop: every later slot also restored. The .md must be
    fully retriable — no temp-dir refs left anywhere."""
    md_with_refs = (
        "![](tmp/a.png)\n\n"
        "![](tmp/b.png)\n\n"
        "![](tmp/c.png)\n\n"
        "![](tmp/d.png)"
    )
    refs = list(pdf2md.IMAGE_REF_RE.finditer(md_with_refs))
    assert len(refs) == 4
    p = "**==> picture [0 x 0] intentionally omitted <==**"

    # Two enriched, then quota -> remaining two restored to placeholders.
    slot_text = ["FIRST OK", "SECOND OK", p, p]
    out = pdf2md._rebuild_md(md_with_refs, refs, slot_text)

    assert "tmp/" not in out
    assert out.count(p) == 2
    assert "FIRST OK" in out and "SECOND OK" in out


def test_placeholder_dims_only_matches_integer_dims():
    md = (
        "**==> picture [120 x 80] intentionally omitted <==**\n"
        "**==> picture [unknown] intentionally omitted <==**\n"
        "**==> picture [3 x 5] intentionally omitted <==**\n"
    )
    sizes = pdf2md.placeholder_dims(md)
    assert sizes == [(120, 80), (3, 5)]


def test_placeholder_re_matches_unknown_token():
    """The recovery placeholder uses [unknown] instead of [W x H]. It must
    still match PLACEHOLDER_RE so a re-run picks it up."""
    md = "**==> picture [unknown] intentionally omitted <==**"
    assert pdf2md.PLACEHOLDER_RE.search(md) is not None


def test_image_ref_re_matches_typical_pymupdf4llm_output():
    """If pymupdf4llm changes its image-ref format we want a loud test
    failure rather than silent bad behavior."""
    samples = [
        "![](img/foo.png)",
        "![alt](img/bar.jpg)",
        "![](C:/temp/thing-0013-07.png)",
    ]
    for s in samples:
        assert pdf2md.IMAGE_REF_RE.search(s) is not None
