"""_validate_args: surface flag combinations that would otherwise be
silently ignored or contradict each other."""
import argparse

import pytest

import pdf2md


def _make_args(**overrides) -> argparse.Namespace:
    """Build a Namespace with every flag set to its parser default, then
    apply overrides. Mirrors what argparse would produce."""
    base = {
        "force": False,
        "jobs": 1,
        "ocr": "auto",
        "ocr_dpi": 150,
        "ocr_lang": "eng",
        "triage": False,
        "triage_only": False,
        "min_chars_per_page": 100,
        "enrich_figures": False,
        "enrich_max_images_per_pdf": 0,
        "enrich_min_image_pixels": [0, 0],
        "enrich_skip_pdfs": None,
        "enrich_dry_run": False,
        "enrich_keep_images": False,
        "enrich_restore_broken": False,
        "enrich_rate_limit_wait": 3600,
        "enrich_max_wait": 14400,
        "enrich_quota_threshold": 0,
        "enrich_pace_aware": False,
        "fallback": "none",
        "api_model": "claude-sonnet-4-6",
        "cli_model": "claude-sonnet-4-6",
        "claude_bin": None,
        "command_file": None,
        "state_dir": None,
        "resume_batch": False,
        "no_triage_cache": False,
        "check": False,
        "root_dir": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_resume_batch_with_processing_flags_errors(capsys):
    args = _make_args(resume_batch=True, triage=True)
    with pytest.raises(SystemExit) as exc_info:
        pdf2md._validate_args(args)
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "--resume-batch runs alone" in captured.out


def test_resume_batch_alone_is_fine():
    args = _make_args(resume_batch=True)
    pdf2md._validate_args(args)  # no exit, no exception


def test_restore_broken_with_processing_flags_errors(capsys):
    args = _make_args(enrich_restore_broken=True, enrich_figures=True)
    with pytest.raises(SystemExit):
        pdf2md._validate_args(args)
    captured = capsys.readouterr()
    assert "--enrich-restore-broken is a recovery-only mode" in captured.out


def test_resume_and_restore_together_errors(capsys):
    args = _make_args(resume_batch=True, enrich_restore_broken=True)
    with pytest.raises(SystemExit):
        pdf2md._validate_args(args)
    captured = capsys.readouterr()
    assert "Pick one" in captured.out


def test_api_model_with_claude_cli_warns(capsys):
    args = _make_args(api_model="custom-model", fallback="claude-cli")
    pdf2md._validate_args(args)
    captured = capsys.readouterr()
    assert "--api-model is ignored" in captured.out


def test_cli_model_with_api_warns(capsys):
    args = _make_args(cli_model="custom-cli", fallback="api")
    pdf2md._validate_args(args)
    captured = capsys.readouterr()
    assert "--cli-model is ignored" in captured.out


def test_triage_only_with_force_warns(capsys):
    args = _make_args(triage_only=True, force=True)
    pdf2md._validate_args(args)
    captured = capsys.readouterr()
    assert "--force has no effect with --triage-only" in captured.out


def test_triage_only_with_jobs_warns(capsys):
    args = _make_args(triage_only=True, jobs=4)
    pdf2md._validate_args(args)
    captured = capsys.readouterr()
    assert "--jobs has no effect with --triage-only" in captured.out


def test_quota_threshold_out_of_range_errors(capsys):
    args = _make_args(enrich_quota_threshold=150)
    with pytest.raises(SystemExit):
        pdf2md._validate_args(args)
    captured = capsys.readouterr()
    assert "--enrich-quota-threshold must be 1-99" in captured.out


def test_clean_args_no_warnings(capsys):
    args = _make_args(triage=True, fallback="batches")
    pdf2md._validate_args(args)
    captured = capsys.readouterr()
    assert captured.out == ""


def test_online_flags_constant_exists():
    """Sanity: ONLINE_FLAGS is the source-of-truth used by --check, --help
    tagging, and validation."""
    assert "fallback" in pdf2md.ONLINE_FLAGS
    assert "enrich_figures" in pdf2md.ONLINE_FLAGS
    assert "force" not in pdf2md.ONLINE_FLAGS
    assert "ocr" not in pdf2md.ONLINE_FLAGS
