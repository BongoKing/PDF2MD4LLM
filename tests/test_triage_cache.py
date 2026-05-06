"""Triage cache: hits avoid PDF I/O; misses recompute and store."""
import time
from pathlib import Path

import pdf2md


def test_cache_key_changes_with_md_mtime(tmp_path: Path):
    pdf_path = tmp_path / "doc.pdf"
    md_path = tmp_path / "doc.md"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    md_path.write_text("# header\n", encoding="utf-8")

    key1 = pdf2md.triage_cache_key(pdf_path, md_path, 100)

    # Touch the .md so its mtime changes; cache key must change too.
    time.sleep(0.01)
    md_path.write_text("# different\n", encoding="utf-8")
    key2 = pdf2md.triage_cache_key(pdf_path, md_path, 100)

    assert key1 != key2


def test_cache_key_changes_with_threshold(tmp_path: Path):
    pdf_path = tmp_path / "doc.pdf"
    md_path = tmp_path / "doc.md"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    md_path.write_text("text", encoding="utf-8")

    assert pdf2md.triage_cache_key(pdf_path, md_path, 100) \
        != pdf2md.triage_cache_key(pdf_path, md_path, 50)


def test_cache_key_handles_missing_md(tmp_path: Path):
    pdf_path = tmp_path / "doc.pdf"
    md_path = tmp_path / "missing.md"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    # Doesn't crash; returns a stable key even when .md is absent.
    key = pdf2md.triage_cache_key(pdf_path, md_path, 100)
    assert isinstance(key, str) and len(key) == 32  # md5 hex


def test_load_save_roundtrip(tmp_path: Path):
    cache_file = tmp_path / "triage_cache_xyz.json"
    payload = {"abc123": {"reasons": ["md-empty"], "should_fallback": True}}

    pdf2md.save_triage_cache(cache_file, payload)
    loaded = pdf2md.load_triage_cache(cache_file)
    assert loaded == payload


def test_load_returns_empty_dict_on_corrupt_file(tmp_path: Path):
    cache_file = tmp_path / "broken.json"
    cache_file.write_text("{not valid json", encoding="utf-8")
    assert pdf2md.load_triage_cache(cache_file) == {}


def test_load_returns_empty_dict_on_missing_file(tmp_path: Path):
    cache_file = tmp_path / "absent.json"
    assert pdf2md.load_triage_cache(cache_file) == {}


def test_triage_uses_cache_on_hit(tmp_path: Path, monkeypatch):
    """Cache hit means analyze_pdf is NOT called — proves we save the
    pymupdf I/O on repeated runs."""
    pdf_path = tmp_path / "doc.pdf"
    md_path = tmp_path / "doc.md"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    md_path.write_text("plenty of text " * 50, encoding="utf-8")

    # Pre-populate the cache for this exact key.
    cache: dict = {}
    key = pdf2md.triage_cache_key(pdf_path, md_path, 100)
    cache[key] = {"reasons": ["math-fonts-dropped (CMMI)"], "should_fallback": True}

    # If the cache is honored, analyze_pdf should never be called.
    calls = {"count": 0}
    def fail_if_called(*args, **kwargs):
        calls["count"] += 1
        return {"pages": 0, "math_fonts": set(), "error": None}
    monkeypatch.setattr(pdf2md, "analyze_pdf", fail_if_called)

    reasons, should_fb = pdf2md.triage(pdf_path, md_path, 100, cache=cache)

    assert calls["count"] == 0
    assert reasons == ["math-fonts-dropped (CMMI)"]
    assert should_fb is True
