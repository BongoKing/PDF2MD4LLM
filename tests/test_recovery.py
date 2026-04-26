"""--enrich-restore-broken: dead refs become [unknown] placeholders."""
from pathlib import Path

import pdf2md


PLACEHOLDER = "**==> picture [unknown] intentionally omitted <==**"


def test_dead_ref_becomes_unknown_placeholder(tmp_path: Path):
    # Live image present alongside the .md.
    live_image = tmp_path / "alive.png"
    live_image.write_bytes(b"\x89PNG\r\n")  # any non-empty bytes

    md = tmp_path / "doc.md"
    md.write_text(
        "# Title\n\n"
        "Live image: ![](alive.png)\n\n"
        "Dead image: ![](does_not_exist.png)\n\n"
        "Original placeholder: **==> picture [120 x 80] intentionally omitted <==**\n",
        encoding="utf-8",
    )

    files_updated, refs_restored = pdf2md.restore_broken_image_refs(tmp_path)

    assert files_updated == 1
    assert refs_restored == 1

    new = md.read_text(encoding="utf-8")
    assert "![](alive.png)" in new            # live ref preserved
    assert "![](does_not_exist.png)" not in new  # dead ref replaced
    assert PLACEHOLDER in new                  # with [unknown] placeholder
    assert "[120 x 80]" in new                 # existing placeholder untouched


def test_idempotent(tmp_path: Path):
    md = tmp_path / "doc.md"
    md.write_text("Dead: ![](missing.png)\n", encoding="utf-8")

    pdf2md.restore_broken_image_refs(tmp_path)
    snapshot = md.read_text(encoding="utf-8")

    files_updated, refs_restored = pdf2md.restore_broken_image_refs(tmp_path)
    assert files_updated == 0
    assert refs_restored == 0
    assert md.read_text(encoding="utf-8") == snapshot


def test_clean_library_is_no_op(tmp_path: Path):
    md = tmp_path / "doc.md"
    md.write_text("# Just text, no images here.\n", encoding="utf-8")

    files_updated, refs_restored = pdf2md.restore_broken_image_refs(tmp_path)
    assert files_updated == 0
    assert refs_restored == 0
