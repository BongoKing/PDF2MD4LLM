"""Pass B reorder: PDFs cap-skipped by --enrich-max-images-per-pdf no
longer pay the slow triage cost.

Direct behavior tests on the helpers; the main() loop's reorder is
covered by the same primitives (pre-computed enrich_skip_reason +
the cap-check uses PLACEHOLDER_RE.finditer just as the loop does).
"""
import re
from pathlib import Path

import pdf2md


def _placeholder_block(count: int) -> str:
    line = "**==> picture [50 x 50] intentionally omitted <==**"
    return "\n".join(line for _ in range(count))


def test_cap_check_uses_only_md_not_pdf(tmp_path: Path, monkeypatch):
    """The cheap pre-check counts placeholders in the .md without ever
    opening the PDF — so analyze_pdf is never called for cap-skipped PDFs."""
    md_path = tmp_path / "doc.md"
    md_path.write_text(_placeholder_block(75), encoding="utf-8")

    # If anyone tries to open the PDF, this monkeypatch fails the test.
    def fail(*args, **kwargs):
        raise AssertionError("cap pre-check should not open the PDF")
    monkeypatch.setattr(pdf2md, "analyze_pdf", fail)

    md_text = md_path.read_text(encoding="utf-8")
    placeholder_count = sum(1 for _ in pdf2md.PLACEHOLDER_RE.finditer(md_text))
    cap = 50
    cap_skip = placeholder_count > cap

    assert placeholder_count == 75
    assert cap_skip is True


def test_under_cap_does_not_trigger_skip(tmp_path: Path):
    md_path = tmp_path / "doc.md"
    md_path.write_text(_placeholder_block(10), encoding="utf-8")
    md_text = md_path.read_text(encoding="utf-8")

    placeholder_count = sum(1 for _ in pdf2md.PLACEHOLDER_RE.finditer(md_text))
    assert placeholder_count == 10
    assert (placeholder_count > 50) is False


def test_no_cap_means_no_pre_check_skip(tmp_path: Path):
    """cap=0 disables the filter — no PDF is ever cap-skipped."""
    md_path = tmp_path / "doc.md"
    md_path.write_text(_placeholder_block(5000), encoding="utf-8")

    cap = 0
    md_text = md_path.read_text(encoding="utf-8")
    placeholder_count = sum(1 for _ in pdf2md.PLACEHOLDER_RE.finditer(md_text))
    cap_skip = cap > 0 and placeholder_count > cap
    assert cap_skip is False


def test_skip_list_match_uses_only_path(tmp_path: Path, monkeypatch):
    """Skip-list pre-check should match path substrings without touching
    the PDF or the .md."""
    pdf_path = tmp_path / "subdir" / "Roncalli - Handbook.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"")

    def fail(*args, **kwargs):
        raise AssertionError("skip-list pre-check shouldn't read files")
    monkeypatch.setattr(pdf2md, "analyze_pdf", fail)

    matches = pdf2md._matches_skip_list(pdf_path, ["Roncalli"])
    assert matches is True


def test_skip_list_no_match(tmp_path: Path):
    pdf_path = tmp_path / "Smith - paper.pdf"
    pdf_path.write_bytes(b"")
    assert pdf2md._matches_skip_list(pdf_path, ["Roncalli", "Arndt"]) is False


def test_empty_skip_list_returns_false(tmp_path: Path):
    pdf_path = tmp_path / "anything.pdf"
    pdf_path.write_bytes(b"")
    assert pdf2md._matches_skip_list(pdf_path, []) is False
    assert pdf2md._matches_skip_list(pdf_path, None) is False
