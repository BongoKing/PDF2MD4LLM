"""Convert PDFs in a directory tree to LLM-friendly Markdown.

Two-stage pipeline:

  1. Local extraction with pymupdf4llm (text, tables, OCR for scanned pages).
  2. Optional Claude pass for content pymupdf4llm cannot recover:
        - Whole-PDF fallback for documents triage flagged as broken.
        - Figure enrichment that transcribes embedded raster images
          (tables, formulas, charts) and splices the result back into
          the existing .md.

The figure-enrichment step is durable: any per-image failure
restores the original placeholder rather than leaving a dead
markdown image ref in the .md, so a subsequent run can retry
cleanly. Quota and rate-limit errors trigger an automatic
sleep-until-reset and resume.
"""

import argparse
import base64
import hashlib
import json
import multiprocessing
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
import pymupdf4llm


# === Constants & regexes ===
# Placeholder text, image-ref grammar, and prompts used throughout the file.

# Math fonts that genuinely indicate mathematical typesetting.
# DO NOT include generic Symbol / SymbolMT / SegoeUISymbol — those are used
# for bullets, arrows and checkmarks in ordinary text PDFs and produce
# heavy false positives.
MATH_FONT_MARKERS = (
    "CMMI", "CMSY", "CMEX", "CMBSY", "CMMIB",        # TeX Computer Modern math
    "MSAM", "MSBM",                                   # AMS math symbols
    "MTMI", "MTSY", "MTEX", "MTSYN",                  # MathTime
    "AdvPSMSAM", "AdvMTSY",                           # AdvancedMath
    "STIXMath", "STIXSizeOneSym", "XITSMath",         # STIX / XITS
    "EulerMath", "AsanaMath", "LatinModernMath",      # OpenType math
    "MathematicalPi",                                 # Adobe Mathematical Pi
    "TeX_CM_Maths",                                   # TeX encoded
)

# If the .md already contains plausible math markup, treat math fonts as
# captured rather than dropped.
LATEX_MARKERS = re.compile(
    r"(\$[^$\n]{2,}\$)|"
    r"(\$\$[\s\S]+?\$\$)|"
    r"(\\(frac|sum|int|sqrt|alpha|beta|gamma|delta|"
    r"theta|lambda|mu|sigma|pi|infty|partial|nabla|"
    r"cdot|times|leq|geq|approx|neq|rightarrow|le|ge))",
    re.IGNORECASE,
)

UNICODE_MATH_CHARS = set("∑∫∂∞≈≠≤≥√πΣΠΔ∇∈∉⊂⊃∪∩⇒⇔→←↔αβγδεζηθικλμνξοπρστυφχψω")

DEFAULT_FALLBACK_PROMPT = (
    "Convert this PDF to LLM-friendly Markdown. "
    "Preserve structure (headings, lists, tables). "
    "Render mathematical formulas as LaTeX: inline as $...$, display as $$...$$. "
    "If the PDF is scanned or image-only, perform OCR. "
    "For figures, include a short descriptive caption in italics. "
    "Output ONLY the Markdown content, no preamble, no code fences around the whole output."
)

# Placeholder pymupdf4llm emits for each embedded image when write_images=False.
# Example: **==> picture [61 x 67] intentionally omitted <==**
PLACEHOLDER_RE = re.compile(
    r"\*\*==>\s*picture\s*\[[^\]]*\]\s*intentionally omitted\s*<==\*\*"
)

# Same placeholder, but capturing W and H — used by the cost / size filter.
# Note: deliberately does NOT match [unknown] placeholders produced by the
# recovery pass (their dims are lost), so those bypass the size filter.
PLACEHOLDER_DIMS_RE = re.compile(
    r"\*\*==>\s*picture\s*\[\s*(\d+)\s*x\s*(\d+)\s*\]\s*"
    r"intentionally omitted\s*<==\*\*"
)

# Markdown image ref produced by pymupdf4llm when write_images=True, plus
# any sentinel refs we write ourselves (batches / command mode).
IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+\.(?:png|jpg|jpeg))\)", re.IGNORECASE)
_FILENAME_SAFE_RE = re.compile(r"[^\w.\- ]")

# Rough Sonnet 4.6 vision cost range per image, batches discount applied.
# Used only by --enrich-dry-run to print a price estimate; tune in one place.
_COST_PER_IMAGE_LOW = 0.005
_COST_PER_IMAGE_HIGH = 0.01

# Pending sentinel format used by batches / command mode to mark a slot
# that will be resolved later. Kept as a valid IMAGE_REF so existing
# tooling treats it consistently; the recovery pass detects it via the
# pdf2md-pending- filename prefix.
SENTINEL_PREFIX = "pdf2md-pending-"

_FILENAME_SAFE_RE = re.compile(r"[^\w.\- ]")

# Rough Sonnet 4.6 vision cost range per image (batches discount applied).
# Used only by --enrich-dry-run to print a price estimate.
_COST_PER_IMAGE_LOW = 0.005
_COST_PER_IMAGE_HIGH = 0.01

# Pending sentinel format used by batches / command mode to mark a slot
# that will be resolved later. Kept as a valid IMAGE_REF so existing
# tooling treats it consistently; the recovery pass detects it via the
# pdf2md-pending- filename prefix.
SENTINEL_PREFIX = "pdf2md-pending-"

_FILENAME_SAFE_RE = re.compile(r"[^\w.\- ]")

# Rough Sonnet 4.6 vision cost range per image (batches discount applied).
# Used only by --enrich-dry-run to print a price estimate.
_COST_PER_IMAGE_LOW = 0.005
_COST_PER_IMAGE_HIGH = 0.01

# Pending sentinel format used by batches / command mode to mark a slot
# that will be resolved later. Kept as a valid IMAGE_REF so existing
# tooling treats it consistently; the recovery pass detects it via the
# pdf2md-pending- filename prefix.
SENTINEL_PREFIX = "pdf2md-pending-"

_FILENAME_SAFE_RE = re.compile(r"[^\w.\- ]")

# Rough Sonnet 4.6 vision cost range per image (batches discount applied).
# Used only by --enrich-dry-run to print a price estimate.
_COST_PER_IMAGE_LOW = 0.005
_COST_PER_IMAGE_HIGH = 0.01

IMAGE_DESCRIBE_PROMPT = (
    "You are viewing an image extracted from a scientific PDF. "
    "Produce the best Markdown representation of its content:\n"
    "- Table -> GitHub-flavored Markdown table, preserve all rows/cells.\n"
    "- Formula -> LaTeX (inline $...$, display $$...$$).\n"
    "- Chart or graph -> 2-3 sentence description of axes and main trend.\n"
    "- Diagram -> 1-2 sentence description of the main elements.\n"
    "- Photograph or decoration -> one italic line starting with *Figure:*.\n"
    "Output ONLY the Markdown replacement. No preamble, no code fence."
)

# Persistent location for kept-image-mode and command-mode image dumps,
# placed under the library root so relative .md links remain valid when
# the library is moved.
IMAGE_ROOT_NAME = "pdf2md_images"

# Project-side state directory layout. Resolved relative to this script
# unless the user overrides with --state-dir.
PROJECT_DIR = Path(__file__).resolve().parent
STATE_DIR_DEFAULT = PROJECT_DIR / "state"
OUTPUT_DIR_DEFAULT = PROJECT_DIR / "output"


def placeholder_dims(md_text: str) -> list[tuple[int, int]]:
    """Pull every (W, H) tuple from placeholders in a .md, in document order."""
    return [(int(w), int(h)) for w, h in PLACEHOLDER_DIMS_RE.findall(md_text)]


def library_hash(root: Path) -> str:
    """10-char md5 of the resolved root path. Used to namespace per-library
    state files so multiple libraries can each have an in-flight batch."""
    return hashlib.md5(str(root.resolve()).encode("utf-8")).hexdigest()[:10]


def batch_state_path(root: Path, state_dir: Path) -> Path:
    return state_dir / f"pdf2md_batch_{library_hash(root)}.json"


def triage_command_path(root: Path, output_dir: Path) -> Path:
    return output_dir / f"pdf2md_triage_commands_{library_hash(root)}.txt"


def triage_cache_path(root: Path, state_dir: Path) -> Path:
    return state_dir / f"triage_cache_{library_hash(root)}.json"


# Flags whose effect requires an LLM call or the Anthropic API.
# The set drives --check output, --help tagging, and validation messages
# so the local-vs-online distinction has a single source of truth.
ONLINE_FLAGS: frozenset[str] = frozenset({
    "fallback",
    "api_model",
    "cli_model",
    "resume_batch",
    "enrich_figures",
    "enrich_rate_limit_wait",
    "enrich_max_wait",
    "enrich_quota_threshold",
    "enrich_pace_aware",
})


# === Tesseract OCR preflight ===

TESSERACT_INSTALL_HINT = """
Tesseract OCR binary not found on PATH.

Install:
  Windows : https://github.com/UB-Mannheim/tesseract/wiki
            or  choco install tesseract-ocr
  macOS   : brew install tesseract
  Linux   : sudo apt install tesseract-ocr

On Windows, add the install dir (default C:\\Program Files\\Tesseract-OCR)
to your PATH and open a new shell, then verify:
  tesseract --version

Bypass this check with  --ocr never  (not recommended for scanned PDFs).
""".strip()


def preflight_tesseract() -> str:
    """Return the Tesseract path or exit with an install hint."""
    path = shutil.which("tesseract")
    if not path:
        print(TESSERACT_INSTALL_HINT)
        sys.exit(1)
    return path


# === Triage ===
# Detect PDFs whose pymupdf4llm output is unusable so the Claude full-PDF
# fallback is only spent where it adds value. The triage cache persists
# results across runs so adding a single new paper to the library doesn't
# trigger a full re-scan.

def triage_cache_key(pdf_path: Path, md_path: Path,
                     min_chars_per_page: int) -> str:
    """Return a stable hash of all inputs that influence triage's verdict.

    A change in any of these invalidates the cache entry:
    - PDF path / mtime / size  (PDF content)
    - .md mtime or "missing"   (extraction result)
    - min_chars_per_page       (the threshold itself)
    """
    try:
        pdf_stat = pdf_path.stat()
        pdf_part = f"{pdf_path.resolve()}|{pdf_stat.st_mtime_ns}|{pdf_stat.st_size}"
    except OSError:
        pdf_part = f"{pdf_path}|missing"
    if md_path.exists():
        md_part = f"{md_path.stat().st_mtime_ns}"
    else:
        md_part = "missing"
    raw = f"{pdf_part}|{md_part}|{min_chars_per_page}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def load_triage_cache(path: Path) -> dict:
    """Read the on-disk cache; tolerant of a missing or corrupt file."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_triage_cache(path: Path, cache: dict) -> None:
    """Write the cache with an atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    tmp.replace(path)


def analyze_pdf(pdf_path: Path) -> dict:
    """Inspect a PDF for math-font presence. pymupdf4llm itself handles
    scanned / image-only documents via OCR."""
    info: dict = {"pages": 0, "math_fonts": set(), "error": None}
    try:
        with pymupdf.open(str(pdf_path)) as doc:
            info["pages"] = doc.page_count
            for page in doc:
                for font in page.get_fonts(full=False):
                    basefont = font[3] if len(font) > 3 else ""
                    for marker in MATH_FONT_MARKERS:
                        if marker.lower() in basefont.lower():
                            info["math_fonts"].add(basefont)
                            break
    except Exception as exc:
        info["error"] = str(exc)
    return info


def md_has_math_markup(md_text: str) -> bool:
    if LATEX_MARKERS.search(md_text):
        return True
    unicode_math = sum(1 for ch in md_text if ch in UNICODE_MATH_CHARS)
    return unicode_math > 5


def triage(pdf_path: Path, md_path: Path,
           min_chars_per_page: int,
           cache: dict | None = None) -> tuple[list[str], bool]:
    """Return (reasons, should_fallback).

    Hard triggers (should_fallback=True):
      - md-empty / md-missing / md-minimal
      - math-fonts-dropped

    Informational (no fallback):
      - math-fonts-present-but-captured

    If `cache` is supplied, the result is looked up by a key built from
    the PDF + .md mtimes/sizes and the threshold. Hits return immediately;
    misses fill the cache in-place (caller persists it once at end of run).
    """
    cache_key = None
    if cache is not None:
        cache_key = triage_cache_key(pdf_path, md_path, min_chars_per_page)
        hit = cache.get(cache_key)
        if hit is not None:
            return list(hit["reasons"]), bool(hit["should_fallback"])

    reasons: list[str] = []
    should_fallback = False

    pdf_info = analyze_pdf(pdf_path)
    if pdf_info["error"]:
        reasons.append(f"pdf-read-error: {pdf_info['error']}")
        if cache is not None and cache_key is not None:
            cache[cache_key] = {"reasons": reasons, "should_fallback": False}
        return reasons, False

    pages = max(pdf_info["pages"], 1)

    md_text = ""
    if md_path.exists():
        md_text = md_path.read_text(encoding="utf-8", errors="ignore")
        md_chars = len(md_text.strip())
        if md_chars == 0:
            reasons.append("md-empty")
            should_fallback = True
        elif md_chars / pages < min_chars_per_page:
            reasons.append(f"md-minimal ({md_chars} chars / {pages} pages)")
            should_fallback = True
    else:
        reasons.append("md-missing")
        should_fallback = True

    if pdf_info["math_fonts"]:
        sample = ", ".join(sorted(pdf_info["math_fonts"])[:2])
        if md_has_math_markup(md_text):
            reasons.append(f"math-fonts-present-but-captured ({sample})")
        else:
            reasons.append(f"math-fonts-dropped ({sample})")
            should_fallback = True

    if cache is not None and cache_key is not None:
        cache[cache_key] = {"reasons": reasons, "should_fallback": should_fallback}
    return reasons, should_fallback


# === Claude CLI resolution ===
# Locate the Claude Code CLI binary across platforms (Windows .cmd / .exe,
# npm install paths, native installer paths) and verify it responds.

CLAUDE_CLI_INSTALL_HINT = """
Claude Code CLI not found. NOTE: the 'Claude' desktop app is a different
product and does NOT provide a `claude` command.

To install the Claude Code CLI:
  npm install -g @anthropic-ai/claude-code
  (requires Node.js; see https://docs.claude.com/en/docs/claude-code/quickstart)

After installing, verify from cmd / PowerShell:
  claude --version

Or pass the binary path explicitly:
  --claude-bin "C:\\path\\to\\claude.cmd"

Alternatives that do NOT require the CLI (use ANTHROPIC_API_KEY instead
of your Claude Max subscription):
  --fallback api       (synchronous)
  --fallback batches   (async, 50% cheaper)
  --fallback command   (write commands to a .txt file, run them later)
""".strip()


def resolve_claude_executable(verify: bool = True) -> str:
    """Find a working `claude` binary, searching PATH and known install dirs."""
    for name in ("claude", "claude.cmd", "claude.exe"):
        path = shutil.which(name)
        if path and (not verify or _verify_claude_binary(path)):
            return path

    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "AnthropicClaude" / "claude.exe",
        Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
        Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd",
        Path.home() / ".npm-global" / "claude.cmd",
        Path.home() / ".claude" / "local" / "claude.exe",
        Path.home() / ".claude" / "local" / "claude.cmd",
    ]
    for candidate in candidates:
        if candidate.exists() and (not verify or _verify_claude_binary(str(candidate))):
            return str(candidate)

    raise RuntimeError(CLAUDE_CLI_INSTALL_HINT)


def _verify_claude_binary(path: str) -> bool:
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        return result.returncode == 0 and "claude" in (result.stdout + result.stderr).lower()
    except Exception:
        return False


def preflight_claude_cli(claude_bin: str | None) -> str:
    """Confirm the CLI is reachable; print version. Exit on failure."""
    if claude_bin:
        if not _verify_claude_binary(claude_bin):
            print(f"Error: --claude-bin {claude_bin!r} did not respond to `--version`.")
            print("Make sure the path points to the Claude Code CLI, not the desktop app.")
            sys.exit(1)
        return claude_bin
    try:
        resolved = resolve_claude_executable(verify=True)
    except RuntimeError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    try:
        out = subprocess.run(
            [resolved, "--version"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        version = (out.stdout or out.stderr).strip().splitlines()[0] \
            if out.stdout or out.stderr else "?"
    except Exception:
        version = "?"
    print(f"Claude Code CLI OK: {resolved}  ({version})")
    return resolved


# === Full-PDF fallback transports ===
# Re-process whole PDFs that triage flagged as broken. Four transports:
# api (sync), claude-cli (Claude Max sub), batches (async/cheaper), command
# (emit shell commands for later execution).

def _build_api_messages(pdf_path: Path) -> list:
    pdf_b64 = base64.standard_b64encode(pdf_path.read_bytes()).decode("utf-8")
    return [{
        "role": "user",
        "content": [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": pdf_b64,
                },
            },
            {"type": "text", "text": DEFAULT_FALLBACK_PROMPT},
        ],
    }]


def fallback_api(pdf_path: Path, md_path: Path, model: str) -> None:
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")

    size_mb = pdf_path.stat().st_size / (1024 * 1024)
    if size_mb > 32:
        raise RuntimeError(f"PDF is {size_mb:.1f} MB, exceeds API 32 MB limit")

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=16000,
        messages=_build_api_messages(pdf_path),
    )
    md_text = "".join(block.text for block in message.content if block.type == "text")
    md_path.write_text(md_text, encoding="utf-8")


def _build_cli_prompt(pdf_path: Path, md_path: Path) -> str:
    return (
        f'Read the PDF at "{pdf_path}" and convert it to '
        f'LLM-friendly Markdown following these rules: {DEFAULT_FALLBACK_PROMPT} '
        f'Write the result to "{md_path}". '
        f"Reply with only 'done' when the file has been written."
    )


def _quote_for_shell(text: str) -> str:
    if os.name == "nt":
        return '"' + text.replace('"', '\\"') + '"'
    return shlex.quote(text)


def build_claude_cli_command(pdf_path: Path, md_path: Path, model: str,
                             claude_bin: str = "claude") -> str:
    prompt = _build_cli_prompt(pdf_path, md_path)
    return (f"{_quote_for_shell(claude_bin)} --model {model} "
            f"--permission-mode acceptEdits --allowedTools Read Write "
            f"-p {_quote_for_shell(prompt)}")


def _claude_cli_argv(pdf_path: Path, md_path: Path, model: str,
                     claude_bin: str) -> list[str]:
    prompt = _build_cli_prompt(pdf_path, md_path)
    return [
        claude_bin,
        "--model", model,
        "--permission-mode", "acceptEdits",
        "--allowedTools", "Read", "Write",
        "-p", prompt,
    ]


def fallback_claude_cli(pdf_path: Path, md_path: Path, model: str,
                        claude_bin: str) -> None:
    argv = _claude_cli_argv(pdf_path, md_path, model, claude_bin)
    print(f"    $ {claude_bin} --model {model} --permission-mode acceptEdits "
          f"--allowedTools Read Write -p <prompt for {pdf_path.name}>")
    result = subprocess.run(
        argv, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or "<no output on stderr or stdout>"
        raise RuntimeError(f"claude -p failed (exit {result.returncode}): {detail}")
    if not md_path.exists():
        stdout = (result.stdout or "").strip()
        raise RuntimeError(
            f"claude -p finished but no .md file was written. stdout: {stdout[:300]!r}"
        )


def fallback_command_only(pdf_path: Path, md_path: Path, model: str, sink) -> None:
    cmd = build_claude_cli_command(pdf_path, md_path, model, claude_bin="claude")
    print(f"    CMD: {cmd}")
    if sink is not None:
        sink.write(cmd + "\n")


# === Batches API state management ===
# fallback_batches_collect / submit_batch / resume_batch operate on a small
# JSON state file under <project>/state/, keyed by library hash.

def _custom_id_for(pdf_path: Path) -> str:
    digest = hashlib.md5(str(pdf_path.resolve()).encode("utf-8")).hexdigest()[:20]
    return f"pdf_{digest}"


def _custom_id_for_image(pdf_path: Path, ref_index: int) -> str:
    key = f"{pdf_path.resolve()}::{ref_index}"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:20]
    return f"img_{digest}"


def fallback_batches_collect(pdf_path: Path, md_path: Path, model: str,
                             batch_state: dict) -> None:
    size_mb = pdf_path.stat().st_size / (1024 * 1024)
    if size_mb > 32:
        raise RuntimeError(f"PDF is {size_mb:.1f} MB, exceeds API 32 MB limit")
    custom_id = _custom_id_for(pdf_path)
    batch_state["requests"].append({
        "custom_id": custom_id,
        "params": {
            "model": model,
            "max_tokens": 16000,
            "messages": _build_api_messages(pdf_path),
        },
    })
    batch_state["mapping"][custom_id] = {"kind": "pdf", "md_path": str(md_path)}


def submit_batch(batch_state: dict, root: Path, state_dir: Path) -> None:
    if not batch_state["requests"]:
        print("No requests to submit.")
        return
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")

    client = anthropic.Anthropic()
    batch = client.messages.batches.create(requests=batch_state["requests"])

    state = {
        "batch_id": batch.id,
        "submitted_at": batch.created_at.isoformat() if hasattr(batch.created_at, "isoformat") else str(batch.created_at),
        "root": str(root.resolve()),
        "mapping": batch_state["mapping"],
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = batch_state_path(root, state_dir)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    print(f"\nBatch submitted: {batch.id}")
    print(f"  Requests: {len(batch_state['requests'])}")
    print(f"  State saved to: {state_path}")
    print(f"  Poll for results with:")
    print(f"    python pdf2md.py \"{root}\" --resume-batch")
    print("  Batches typically finish within 1 hour (max 24h, 50% cheaper than sync).")


def resume_batch(root: Path, state_dir: Path) -> None:
    state_path = batch_state_path(root, state_dir)
    if not state_path.exists():
        print(f"No batch state file at {state_path}")
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))

    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")

    client = anthropic.Anthropic()
    batch_id = state["batch_id"]
    batch = client.messages.batches.retrieve(batch_id)
    print(f"Batch {batch_id}: processing_status={batch.processing_status}")

    if batch.processing_status != "ended":
        counts = getattr(batch, "request_counts", None)
        if counts:
            print(f"  counts: {counts}")
        print("Not finished yet. Re-run later.")
        return

    ok = failed = 0
    image_edits: dict[str, list[tuple[str, str]]] = {}

    for result in client.messages.batches.results(batch_id):
        entry = state["mapping"].get(result.custom_id)
        if entry is None:
            continue

        if result.result.type != "succeeded":
            failed += 1
            print(f"  FAILED {result.custom_id}: {result.result}")
            continue

        text = "".join(
            block.text for block in result.result.message.content if block.type == "text"
        ).strip()

        kind = entry.get("kind", "pdf")
        md_path = Path(entry["md_path"])
        if kind == "pdf":
            md_path.parent.mkdir(parents=True, exist_ok=True)
            md_path.write_text(text, encoding="utf-8")
            ok += 1
        elif kind == "image":
            image_edits.setdefault(str(md_path), []).append((entry["image_ref"], text))
            ok += 1

    for md_path_str, edits in image_edits.items():
        md_path = Path(md_path_str)
        if not md_path.exists():
            continue
        content = md_path.read_text(encoding="utf-8", errors="ignore")
        for ref, replacement in edits:
            content = content.replace(ref, replacement, 1)
        md_path.write_text(content, encoding="utf-8")

    print(f"\nBatch results applied: ok={ok}, failed={failed}")
    state_path.rename(state_path.with_name(state_path.name + ".done"))


# === Quota & pacing ===
# Detect rate-limit errors from API and CLI, extract retry-after hints,
# and sleep-with-cap so an overnight run can resume after a quota window
# closes without manual intervention.

class QuotaExhaustedError(RuntimeError):
    """Raised when the run-wide max_wait cap is exceeded.

    The slot-based rebuild restores any unprocessed images to their
    original placeholders before this propagates, so a re-run picks up
    cleanly where the run stopped.
    """


_QUOTA_MARKERS = (
    "rate limit", "rate_limit", "rate-limit",
    "quota", "usage limit", "usage_limit",
    "429", "too many requests",
    "credit balance is too low",
    "overloaded", "overloaded_error",
)


def is_quota_or_rate_limit(error_text: str) -> bool:
    """True if the error string looks like a rate-limit / quota / overload."""
    haystack = (error_text or "").lower()
    return any(marker in haystack for marker in _QUOTA_MARKERS)


# Recognised retry-after hints in stderr / error bodies.
_RETRY_AFTER_RE = re.compile(
    r"retry[\s_-]*after[\s:=]+(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_ISO_RESET_RE = re.compile(
    r"(reset|resets|retry)[^0-9]{0,30}"
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)",
    re.IGNORECASE,
)


def parse_retry_after(text: str) -> float | None:
    """Pick a wait-seconds hint out of an error message, or None.

    Recognises:
      - HTTP-style Retry-After: <seconds>
      - ISO-8601 reset timestamps embedded in error bodies
    """
    if not text:
        return None
    match = _RETRY_AFTER_RE.search(text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    match = _ISO_RESET_RE.search(text)
    if match:
        timestamp = match.group(2).rstrip("Z")
        try:
            from datetime import datetime, timezone
            if "+" in timestamp or timestamp[-6] in ("+", "-"):
                target = datetime.fromisoformat(timestamp)
            else:
                target = datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = (target - now).total_seconds()
            return max(delta, 0.0)
        except (ValueError, IndexError):
            pass
    return None


@dataclass
class RateLimitSnapshot:
    """API-mode pacing data harvested from anthropic-ratelimit-* headers."""
    requests_used_pct: float
    tokens_used_pct: float
    requests_reset: str
    tokens_reset: str

    def max_used_pct(self) -> float:
        return max(self.requests_used_pct, self.tokens_used_pct)

    def soonest_reset(self) -> str:
        return self.requests_reset if self.requests_used_pct >= self.tokens_used_pct \
            else self.tokens_reset


def read_rate_limit_headers(headers) -> RateLimitSnapshot | None:
    """Parse anthropic-ratelimit-* headers into a snapshot, or None if absent."""
    def get(name: str) -> str | None:
        try:
            return headers.get(name)
        except AttributeError:
            return headers[name] if name in headers else None

    try:
        req_limit = float(get("anthropic-ratelimit-requests-limit") or 0)
        req_remaining = float(get("anthropic-ratelimit-requests-remaining") or 0)
        tok_limit = float(get("anthropic-ratelimit-tokens-limit") or 0)
        tok_remaining = float(get("anthropic-ratelimit-tokens-remaining") or 0)
    except (TypeError, ValueError):
        return None
    if req_limit == 0 or tok_limit == 0:
        return None
    return RateLimitSnapshot(
        requests_used_pct=100.0 * (1.0 - req_remaining / req_limit),
        tokens_used_pct=100.0 * (1.0 - tok_remaining / tok_limit),
        requests_reset=get("anthropic-ratelimit-requests-reset") or "",
        tokens_reset=get("anthropic-ratelimit-tokens-reset") or "",
    )


@dataclass
class RateLimitTracker:
    """Run-wide pacing state. One instance per --enrich-figures invocation."""
    max_wait_seconds: float
    rate_limit_wait_seconds: float
    sleep_func: callable = field(default=time.sleep)
    total_slept: float = 0.0
    last_snapshot: RateLimitSnapshot | None = None
    previous_snapshot: RateLimitSnapshot | None = None

    def sleep_until_reset(self, wait_seconds: float, reason: str) -> None:
        """Sleep, accumulating into total_slept. Raises QuotaExhaustedError
        if the run-wide cap would be exceeded."""
        wait_seconds = max(wait_seconds, 1.0)
        if self.total_slept + wait_seconds > self.max_wait_seconds:
            raise QuotaExhaustedError(
                f"Run-wide --enrich-max-wait ({_fmt_duration(self.max_wait_seconds)}) "
                f"would be exceeded by sleeping another {_fmt_duration(wait_seconds)}. "
                f"Total already slept: {_fmt_duration(self.total_slept)}."
            )
        print(f"=== {reason} ===")
        print(f"   Sleeping {_fmt_duration(wait_seconds)}, then retrying...")
        self.sleep_func(wait_seconds)
        self.total_slept += wait_seconds
        print(f"=== Resuming (total slept this run: "
              f"{_fmt_duration(self.total_slept)}) ===")

    def remember(self, snapshot: RateLimitSnapshot | None) -> None:
        if snapshot is None:
            return
        self.previous_snapshot = self.last_snapshot
        self.last_snapshot = snapshot

    def projected_to_cross(self, threshold_pct: float, lookahead_calls: int = 5) -> bool:
        """True if pace-aware mode predicts the threshold will be crossed within
        the next `lookahead_calls` API calls based on the slope of usage."""
        if self.last_snapshot is None or self.previous_snapshot is None:
            return False
        slope = (self.last_snapshot.tokens_used_pct
                 - self.previous_snapshot.tokens_used_pct)
        if slope <= 0:
            return False
        projected = self.last_snapshot.tokens_used_pct + slope * lookahead_calls
        return projected >= threshold_pct


def call_with_retry(func, *args, retries: int = 2, base_delay: float = 2.0,
                    **kwargs):
    """Run func with exponential backoff for transient errors.

    Quota / rate-limit errors are NOT retried here; they propagate unchanged
    so the caller can apply its own slot-restore + sleep-and-retry policy.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            if is_quota_or_rate_limit(str(exc)):
                raise
            last_exc = exc
            if attempt >= retries:
                break
            time.sleep(base_delay * (2 ** attempt))
    assert last_exc is not None
    raise last_exc


# === Figure enrichment ===
# Re-extract a PDF with write_images=True, hand each embedded image to
# Claude with an image-specific prompt, and splice the response back into
# the .md. The slot-based rebuild guarantees every slot is either
# enriched or restored to its original placeholder, so a partial run
# leaves the .md fully retriable.

def _ocr_kwargs(ocr_mode: str, ocr_dpi: int, ocr_lang: str) -> dict:
    if ocr_mode == "never":
        return {}
    kwargs = {"use_ocr": True, "ocr_dpi": ocr_dpi, "ocr_language": ocr_lang}
    if ocr_mode == "always":
        kwargs["force_ocr"] = True
    return kwargs


def _build_image_api_messages(image_path: Path) -> list:
    img_b64 = base64.standard_b64encode(image_path.read_bytes()).decode("utf-8")
    ext = image_path.suffix.lower().lstrip(".")
    media_type = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(
        ext, "image/png")
    return [{
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": img_b64,
                },
            },
            {"type": "text", "text": IMAGE_DESCRIBE_PROMPT},
        ],
    }]


def _enrich_image_api(image_path: Path, model: str
                      ) -> tuple[str, RateLimitSnapshot | None]:
    """Return (transcription, rate-limit snapshot). Reads the raw response
    so anthropic-ratelimit-* headers are available for pacing."""
    import anthropic
    client = anthropic.Anthropic()
    raw = client.messages.with_raw_response.create(
        model=model,
        max_tokens=4000,
        messages=_build_image_api_messages(image_path),
    )
    snapshot = read_rate_limit_headers(raw.headers)
    message = raw.parse()
    text = "".join(block.text for block in message.content if block.type == "text").strip()
    return text, snapshot


def _enrich_image_cli(image_path: Path, model: str, claude_bin: str) -> str:
    """Run claude -p on the image. Returns transcription text.

    Copies the image to a shell-safe filename when needed (the Read tool
    cannot resolve names containing apostrophes or other shell-hostile
    characters such as a curly apostrophe in a PDF title).
    """
    safe_path: Path | None = None
    work_path = image_path
    if _FILENAME_SAFE_RE.search(image_path.name):
        safe_name = _FILENAME_SAFE_RE.sub("_", image_path.name)
        safe_path = image_path.parent / safe_name
        shutil.copy2(image_path, safe_path)
        work_path = safe_path

    try:
        prompt = f'Read the image at "{work_path}" and perform this task: {IMAGE_DESCRIBE_PROMPT}'
        argv = [
            claude_bin, "--model", model,
            "--permission-mode", "acceptEdits",
            "--allowedTools", "Read",
            "--add-dir", str(work_path.parent),
            "-p", prompt,
        ]
        result = subprocess.run(
            argv, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or "<no output>"
            raise RuntimeError(f"claude -p failed (exit {result.returncode}): {detail}")
        return (result.stdout or "").strip()
    finally:
        if safe_path is not None:
            safe_path.unlink(missing_ok=True)


def _build_image_cli_command(image_path: Path, model: str,
                             claude_bin: str = "claude") -> str:
    prompt = f'Read the image at "{image_path}" and perform this task: {IMAGE_DESCRIBE_PROMPT}'
    return (f"{_quote_for_shell(claude_bin)} --model {model} "
            f"--permission-mode acceptEdits --allowedTools Read "
            f"--add-dir {_quote_for_shell(str(image_path.parent))} "
            f"-p {_quote_for_shell(prompt)}")


def _matches_skip_list(pdf_path: Path, skip_substrings: list) -> bool:
    if not skip_substrings:
        return False
    text = str(pdf_path)
    return any(needle and needle in text for needle in skip_substrings)


def _sentinel_ref_for(custom_id: str) -> str:
    return f"![{SENTINEL_PREFIX}{custom_id}]({SENTINEL_PREFIX}{custom_id}.png)"


def _figure_footer(image_full_path: Path, md_path: Path) -> str:
    """Markdown footer line pointing at the kept image, relative to the .md."""
    rel = os.path.relpath(image_full_path, md_path.parent)
    rel = rel.replace("\\", "/")
    return f"\n\n*Source figure:* ![]({rel})"


def _rebuild_md(md_with_refs: str, refs: list, slot_text: list[str]) -> str:
    """Reconstruct the .md by substituting each ref with slot_text[i].

    Uses match positions rather than `replace()` so that two structurally
    identical refs are still substituted independently and in order.
    """
    out: list[str] = []
    cursor = 0
    for match, replacement in zip(refs, slot_text):
        out.append(md_with_refs[cursor:match.start()])
        out.append(replacement)
        cursor = match.end()
    out.append(md_with_refs[cursor:])
    return "".join(out)


def enrich_figures_for_pdf(
    pdf_path: Path,
    md_path: Path,
    mode: str,
    api_model: str,
    cli_model: str,
    claude_bin: str,
    ocr_kwargs: dict,
    command_sink,
    batch_state: dict,
    image_root: Path,
    filters: dict,
    keep_images: bool,
    rate_limit_tracker: RateLimitTracker,
    quota_threshold: int,
    pace_aware: bool,
) -> tuple[int, int, int, str | None]:
    """Enrich a single PDF's .md by transcribing every embedded image.

    Returns (images_processed, images_failed, images_dropped_size, skip_reason).
    skip_reason is None, "skip-list", or "cap-exceeded".

    Raises QuotaExhaustedError if the run-wide max_wait cap is hit; the .md
    is written in a fully retriable state before the exception propagates.
    """
    if not md_path.exists():
        return 0, 0, 0, None
    original_md = md_path.read_text(encoding="utf-8", errors="ignore")
    original_placeholders = [m.group(0) for m in PLACEHOLDER_RE.finditer(original_md)]
    if not original_placeholders:
        return 0, 0, 0, None

    if _matches_skip_list(pdf_path, filters.get("skip_substrings") or []):
        return 0, 0, 0, "skip-list"

    cap = filters.get("max_per_pdf") or 0
    if cap > 0 and len(original_placeholders) > cap:
        return 0, 0, 0, "cap-exceeded"

    placeholder_sizes = placeholder_dims(original_md)
    has_dims = len(placeholder_sizes) == len(original_placeholders)

    min_w = filters.get("min_w") or 0
    min_h = filters.get("min_h") or 0
    if has_dims:
        survivor_indices = [
            i for i, (w, h) in enumerate(placeholder_sizes)
            if w >= min_w and h >= min_h
        ]
        dropped = len(original_placeholders) - len(survivor_indices)
    else:
        # Some placeholders are [unknown] (recovery output); they bypass the
        # size filter and all enter the run.
        survivor_indices = list(range(len(original_placeholders)))
        dropped = 0
    if not survivor_indices:
        return 0, 0, dropped, None

    # Persistent image dir for command mode and --enrich-keep-images,
    # otherwise a temp dir we wipe in finally.
    use_persistent = (mode == "command") or keep_images
    if use_persistent:
        pdf_hash = hashlib.md5(str(pdf_path.resolve()).encode("utf-8")).hexdigest()[:10]
        img_dir = image_root / f"{pdf_path.stem}_{pdf_hash}"
        img_dir.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        img_dir = Path(tempfile.mkdtemp(prefix="pdf2md_imgs_"))
        cleanup = True

    write_md_now = False  # set True on QuotaExhaustedError to flush the .md
    quota_exception: QuotaExhaustedError | None = None

    try:
        md_with_refs = pymupdf4llm.to_markdown(
            str(pdf_path),
            write_images=True,
            image_path=str(img_dir),
            image_format="png",
            **ocr_kwargs,
        )
        refs = list(IMAGE_REF_RE.finditer(md_with_refs))
        if not refs:
            return 0, 0, dropped, None

        # The placeholder count from pymupdf4llm should equal the ref count;
        # if it diverges, process all refs (no size filter) rather than
        # mis-matching placeholders to images.
        if len(refs) != len(original_placeholders):
            print(f"      WARN: ref count mismatch for {pdf_path.name} "
                  f"(placeholders={len(original_placeholders)}, refs={len(refs)}). "
                  f"Processing all refs without size filter.")
            chosen = list(range(len(refs)))
            dropped = 0
        else:
            chosen = [i for i in survivor_indices if i < len(refs)]

        chosen_set = set(chosen)
        # Default: every slot keeps its original placeholder. Filtered-out
        # slots remain as placeholders; chosen slots will be replaced with
        # transcription (success), sentinel (pending), or placeholder again
        # (failure / quota).
        slot_text: list[str] = list(original_placeholders) \
            if len(original_placeholders) == len(refs) \
            else [f"![]({m.group(1)})" for m in refs]

        if mode in ("api", "claude-cli"):
            processed = failed = 0
            total = len(chosen)
            for i, ref_idx in enumerate(chosen, 1):
                match = refs[ref_idx]
                img_path = Path(match.group(1))
                if not img_path.is_absolute():
                    img_path = (img_dir / img_path.name).resolve()
                start = time.time()

                done = False
                while not done:
                    try:
                        if mode == "api":
                            response, snapshot = _enrich_image_api(img_path, api_model)
                            rate_limit_tracker.remember(snapshot)
                        else:
                            response = _enrich_image_cli(img_path, cli_model, claude_bin)

                        slot_text[ref_idx] = response + (
                            _figure_footer(img_path, md_path) if keep_images else ""
                        )
                        processed += 1
                        print(f"      [img {i}/{total}] {img_path.name} done "
                              f"({_fmt_duration(time.time() - start)})")
                        done = True

                        # Threshold check on the freshly-acquired snapshot.
                        if (mode == "api" and quota_threshold > 0
                                and rate_limit_tracker.last_snapshot is not None
                                and rate_limit_tracker.last_snapshot.max_used_pct()
                                >= quota_threshold):
                            snap = rate_limit_tracker.last_snapshot
                            rate_limit_tracker.sleep_until_reset(
                                _wait_from_reset(snap.soonest_reset(),
                                                 rate_limit_tracker.rate_limit_wait_seconds),
                                f"Quota threshold reached: {snap.max_used_pct():.0f}% used"
                                f" (resets at {snap.soonest_reset()})",
                            )
                        elif (mode == "api" and quota_threshold > 0 and pace_aware
                              and rate_limit_tracker.projected_to_cross(quota_threshold)):
                            snap = rate_limit_tracker.last_snapshot
                            assert snap is not None
                            rate_limit_tracker.sleep_until_reset(
                                _wait_from_reset(snap.soonest_reset(),
                                                 rate_limit_tracker.rate_limit_wait_seconds),
                                f"Pace-aware: projecting to cross {quota_threshold}%"
                                f" within next 5 calls",
                            )

                    except Exception as exc:
                        err_text = str(exc)
                        if is_quota_or_rate_limit(err_text):
                            wait = parse_retry_after(err_text)
                            # API mode: try the response object's headers too
                            response_obj = getattr(exc, "response", None)
                            if wait is None and response_obj is not None:
                                try:
                                    snapshot = read_rate_limit_headers(
                                        response_obj.headers)
                                    if snapshot is not None:
                                        wait = _wait_from_reset(
                                            snapshot.soonest_reset(),
                                            rate_limit_tracker.rate_limit_wait_seconds,
                                        )
                                except Exception:
                                    pass
                            if wait is None:
                                wait = rate_limit_tracker.rate_limit_wait_seconds
                            try:
                                rate_limit_tracker.sleep_until_reset(
                                    wait,
                                    f"Quota hit on {pdf_path.name} "
                                    f"img {i}/{total}",
                                )
                            except QuotaExhaustedError as quota_exc:
                                # Restore remaining slots (this one + later) to
                                # placeholders, mark md for write, propagate.
                                for later_idx in chosen[i - 1:]:
                                    slot_text[later_idx] = original_placeholders[later_idx]
                                quota_exception = quota_exc
                                write_md_now = True
                                done = True  # break inner loop
                            # else: loop and retry the same image
                        else:
                            print(f"      [img {i}/{total}] enrich error on "
                                  f"{img_path.name}: {exc}")
                            failed += 1
                            slot_text[ref_idx] = original_placeholders[ref_idx]
                            done = True

                if quota_exception is not None:
                    break

            new_md = _rebuild_md(md_with_refs, refs, slot_text)
            md_path.write_text(new_md, encoding="utf-8")

            if quota_exception is not None:
                raise quota_exception
            return processed, failed, dropped, None

        if mode == "batches":
            queued = 0
            for ref_idx in chosen:
                match = refs[ref_idx]
                img_path = Path(match.group(1))
                if not img_path.is_absolute():
                    img_path = (img_dir / img_path.name).resolve()
                custom_id = _custom_id_for_image(pdf_path, ref_idx)
                sentinel = _sentinel_ref_for(custom_id)
                batch_state["requests"].append({
                    "custom_id": custom_id,
                    "params": {
                        "model": api_model,
                        "max_tokens": 4000,
                        "messages": _build_image_api_messages(img_path),
                    },
                })
                batch_state["mapping"][custom_id] = {
                    "kind": "image",
                    "md_path": str(md_path),
                    "image_ref": sentinel,
                }
                slot_text[ref_idx] = sentinel + (
                    _figure_footer(img_path, md_path) if keep_images else ""
                )
                queued += 1
            new_md = _rebuild_md(md_with_refs, refs, slot_text)
            md_path.write_text(new_md, encoding="utf-8")
            return queued, 0, dropped, None

        if mode == "command":
            queued = 0
            for ref_idx in chosen:
                match = refs[ref_idx]
                img_path = Path(match.group(1))
                if not img_path.is_absolute():
                    img_path = (img_dir / img_path.name).resolve()
                if _FILENAME_SAFE_RE.search(img_path.name):
                    safe_name = _FILENAME_SAFE_RE.sub("_", img_path.name)
                    safe_path = img_path.parent / safe_name
                    if not safe_path.exists():
                        shutil.copy2(img_path, safe_path)
                    img_path = safe_path
                cmd = _build_image_cli_command(img_path, cli_model)
                print(f"    CMD: {cmd}")
                if command_sink is not None:
                    command_sink.write(cmd + "\n")
                custom_id = _custom_id_for_image(pdf_path, ref_idx)
                sentinel = _sentinel_ref_for(custom_id)
                slot_text[ref_idx] = sentinel + (
                    _figure_footer(img_path, md_path) if keep_images else ""
                )
                queued += 1
            new_md = _rebuild_md(md_with_refs, refs, slot_text)
            md_path.write_text(new_md, encoding="utf-8")
            return queued, 0, dropped, None

        raise ValueError(f"Unknown enrich mode: {mode}")
    finally:
        if cleanup and not write_md_now:
            shutil.rmtree(img_dir, ignore_errors=True)
        elif cleanup and write_md_now:
            shutil.rmtree(img_dir, ignore_errors=True)


def _wait_from_reset(reset_iso: str, fallback_seconds: float) -> float:
    """Convert an ISO-8601 reset timestamp into seconds-from-now, with a
    fallback when the timestamp is missing or unparseable."""
    if not reset_iso:
        return fallback_seconds
    try:
        from datetime import datetime, timezone
        cleaned = reset_iso.rstrip("Z")
        if cleaned.endswith("+00:00") or cleaned.count("+") + cleaned.count("-") >= 2:
            target = datetime.fromisoformat(cleaned)
        else:
            target = datetime.fromisoformat(cleaned).replace(tzinfo=timezone.utc)
        delta = (target - datetime.now(timezone.utc)).total_seconds()
        return max(delta, 1.0)
    except Exception:
        return fallback_seconds


# === Recovery ===
# Convert dead markdown image refs (pointing at files that no longer exist
# on disk) back to retriable placeholders so a subsequent --enrich-figures
# pass picks them up.

def restore_broken_image_refs(root: Path) -> tuple[int, int]:
    """Walk root, replace IMAGE_REFs whose target file is missing with the
    [unknown] placeholder. Returns (md_files_updated, refs_restored)."""
    files_updated = 0
    refs_restored = 0
    placeholder_unknown = "**==> picture [unknown] intentionally omitted <==**"

    md_files = sorted(root.rglob("*.md"))
    for md_path in md_files:
        try:
            text = md_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if "![" not in text:
            continue

        original = text
        rewritten_parts: list[str] = []
        cursor = 0
        local_refs_restored = 0

        for match in IMAGE_REF_RE.finditer(text):
            ref_target = match.group(1)
            target_path = Path(ref_target)
            if not target_path.is_absolute():
                target_path = (md_path.parent / target_path).resolve()

            if target_path.exists():
                continue

            rewritten_parts.append(text[cursor:match.start()])
            rewritten_parts.append(placeholder_unknown)
            cursor = match.end()
            local_refs_restored += 1

        if local_refs_restored == 0:
            continue
        rewritten_parts.append(text[cursor:])
        new_text = "".join(rewritten_parts)
        if new_text != original:
            md_path.write_text(new_text, encoding="utf-8")
            files_updated += 1
            refs_restored += local_refs_restored
            try:
                rel = md_path.relative_to(root)
            except ValueError:
                rel = md_path
            print(f"  restored {local_refs_restored} broken ref(s) in {rel}")

    return files_updated, refs_restored


# === Pipeline ===
# Wallclock helpers, per-PDF conversion worker, dry-run, dispatch.

def convert_with_pymupdf4llm(pdf_path: Path, md_path: Path, ocr_kwargs: dict) -> None:
    md_text = pymupdf4llm.to_markdown(str(pdf_path), **ocr_kwargs)
    md_path.write_text(md_text, encoding="utf-8")


def _convert_worker(task: tuple) -> tuple:
    """Module-level worker for multiprocessing.Pool (Windows spawn requires
    this to be picklable). Returns (pdf_path_str, status, err_or_none)."""
    pdf_path_str, md_path_str, ocr_kwargs = task
    try:
        md_text = pymupdf4llm.to_markdown(pdf_path_str, **ocr_kwargs)
        Path(md_path_str).write_text(md_text, encoding="utf-8")
        return (pdf_path_str, "ok", None)
    except Exception as exc:
        return (pdf_path_str, "error", str(exc))


def _fmt_duration(seconds: float) -> str:
    """Render seconds as 'Xs', 'Xm Ys', or 'Xh Ym'."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _progress_suffix(start_time: float, done: int, total: int) -> str:
    """'(elapsed 2m 30s, ETA 5m 10s)' from rolling wallclock average."""
    if done == 0 or total == 0:
        return ""
    elapsed = time.time() - start_time
    remaining = total - done
    eta = (elapsed / done) * remaining if remaining > 0 else 0
    return f"(elapsed {_fmt_duration(elapsed)}, ETA {_fmt_duration(eta)})"


def dispatch_fallback(
    pdf_path: Path,
    md_path: Path,
    mode: str,
    api_model: str,
    cli_model: str,
    claude_bin: str,
    command_sink,
    batch_state: dict,
) -> None:
    if mode == "api":
        fallback_api(pdf_path, md_path, api_model)
    elif mode == "claude-cli":
        fallback_claude_cli(pdf_path, md_path, cli_model, claude_bin)
    elif mode == "command":
        fallback_command_only(pdf_path, md_path, cli_model, command_sink)
    elif mode == "batches":
        fallback_batches_collect(pdf_path, md_path, api_model, batch_state)
    else:
        raise ValueError(f"Unknown fallback mode: {mode}")


def _sample_runtime_estimate(
    survivors: list,
    mode: str,
    api_model: str,
    cli_model: str,
    claude_bin: str,
    ocr_kwargs: dict,
    sample_size: int = 5,
) -> tuple[float, float] | None:
    """Sample N PDFs, time their re-extraction + one image call each, then
    extrapolate to the whole survivor set. Makes real Claude calls."""
    eligible = [s for s in survivors if s[2]]
    if not eligible:
        return None
    n = min(sample_size, len(eligible))
    sample = random.sample(eligible, n)

    extract_times: list[float] = []
    image_times: list[float] = []

    print(f"\nSampling per-image timing: {n} PDF(s), 1 image each "
          f"(real Claude calls via --fallback {mode})...")

    for i, (pdf_path, _md_path, surv_idx) in enumerate(sample, 1):
        print(f"  [{i}/{n}] {pdf_path.name}", flush=True)
        try:
            with tempfile.TemporaryDirectory(prefix="pdf2md_dryimgs_") as tmp_str:
                tmp = Path(tmp_str)
                start = time.time()
                try:
                    md_text = pymupdf4llm.to_markdown(
                        str(pdf_path), write_images=True,
                        image_path=str(tmp), image_format="png",
                        **ocr_kwargs,
                    )
                except Exception as exc:
                    print(f"      re-extract failed: {exc}")
                    continue
                extract_dt = time.time() - start
                extract_times.append(extract_dt)
                print(f"      re-extract: {_fmt_duration(extract_dt)}")

                refs = list(IMAGE_REF_RE.finditer(md_text))
                valid_idxs = [j for j in surv_idx if j < len(refs)]
                if not valid_idxs:
                    continue
                ref_idx = random.choice(valid_idxs)
                img_path = Path(refs[ref_idx].group(1))
                if not img_path.is_absolute():
                    img_path = (tmp / img_path.name).resolve()
                if not img_path.exists():
                    continue

                start = time.time()
                try:
                    if mode == "claude-cli":
                        _enrich_image_cli(img_path, cli_model, claude_bin)
                    elif mode == "api":
                        _enrich_image_api(img_path, api_model)
                    else:
                        return None
                    img_dt = time.time() - start
                    image_times.append(img_dt)
                    print(f"      image call: {_fmt_duration(img_dt)}")
                except Exception as exc:
                    print(f"      image call failed: {exc}")
        except Exception as exc:
            print(f"      sample error: {exc}")

    if not extract_times or not image_times:
        return None

    total_pdfs = sum(1 for s in survivors if s[2])
    total_images = sum(len(s[2]) for s in survivors)

    extract_lo = min(extract_times)
    extract_hi = max(extract_times)
    image_lo = min(image_times)
    image_hi = max(image_times)

    # If we only got one sample, give a +/-20% margin instead of a flat range.
    if len(extract_times) == 1:
        extract_lo *= 0.8
        extract_hi *= 1.2
    if len(image_times) == 1:
        image_lo *= 0.8
        image_hi *= 1.2

    low_total = total_pdfs * extract_lo + total_images * image_lo
    high_total = total_pdfs * extract_hi + total_images * image_hi
    return (low_total, high_total)


def enrich_figures_dry_run(
    pdf_files: list,
    root: Path,
    filters: dict,
    mode: str = "none",
    api_model: str = "",
    cli_model: str = "",
    claude_bin: str = "claude",
    ocr_kwargs: dict | None = None,
) -> None:
    """Iterate the library counting what enrichment WOULD do. Reads existing
    .md files only (no PDF re-extraction) for the cost report. If `mode` is
    'claude-cli' or 'api', additionally samples 5 PDFs to estimate runtime."""
    if ocr_kwargs is None:
        ocr_kwargs = {}
    skip_subs = filters.get("skip_substrings") or []
    cap = filters.get("max_per_pdf") or 0
    min_w = filters.get("min_w") or 0
    min_h = filters.get("min_h") or 0

    candidate_pdfs = 0
    skipped_skiplist: list[tuple[Path, int]] = []
    skipped_cap: list[tuple[Path, int]] = []
    total_placeholders = 0
    images_in_skip_pdfs = 0
    images_in_cap_pdfs = 0
    images_dropped_size = 0
    images_to_enrich = 0
    pdfs_to_enrich = 0
    survivors: list[tuple[Path, Path, list[int]]] = []

    for pdf_path in pdf_files:
        md_path = pdf_path.with_suffix(".md")
        if not md_path.exists():
            continue
        try:
            md = md_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        sizes = placeholder_dims(md)
        if not sizes:
            continue
        candidate_pdfs += 1
        total_placeholders += len(sizes)

        if _matches_skip_list(pdf_path, skip_subs):
            skipped_skiplist.append((pdf_path, len(sizes)))
            images_in_skip_pdfs += len(sizes)
            continue

        if cap > 0 and len(sizes) > cap:
            skipped_cap.append((pdf_path, len(sizes)))
            images_in_cap_pdfs += len(sizes)
            continue

        survivor_idx = [i for i, (w, h) in enumerate(sizes)
                        if w >= min_w and h >= min_h]
        images_dropped_size += len(sizes) - len(survivor_idx)
        images_to_enrich += len(survivor_idx)
        if survivor_idx:
            pdfs_to_enrich += 1
            survivors.append((pdf_path, md_path, survivor_idx))

    print()
    print("=== Enrich-figures dry run ===")
    print(f"Candidate PDFs (have placeholders):  {candidate_pdfs}")
    if skipped_skiplist:
        print(f"  - skipped by --enrich-skip-pdfs:    {len(skipped_skiplist):>6}  "
              f"({images_in_skip_pdfs} placeholders)")
    if skipped_cap:
        print(f"  - skipped by per-PDF cap (>{cap}):    {len(skipped_cap):>6}  "
              f"({images_in_cap_pdfs} placeholders)")
        worst = sorted(skipped_cap, key=lambda x: -x[1])[:5]
        for path, count in worst:
            try:
                rel = path.relative_to(root)
            except ValueError:
                rel = path
            print(f"      {count:>5} placeholders  {rel}")
    print(f"Total placeholders found:           {total_placeholders:>7}")
    if images_in_skip_pdfs:
        print(f"  - in skip-list PDFs:              {images_in_skip_pdfs:>7}")
    if images_in_cap_pdfs:
        print(f"  - in capped PDFs:                 {images_in_cap_pdfs:>7}")
    if images_dropped_size:
        print(f"  - dropped by --enrich-min-image-pixels (>={min_w}x{min_h}): "
              f"{images_dropped_size:>7}")
    print(f"Would enrich:                       {images_to_enrich:>7}  images "
          f"across {pdfs_to_enrich} PDFs")

    low = images_to_enrich * _COST_PER_IMAGE_LOW
    high = images_to_enrich * _COST_PER_IMAGE_HIGH
    print(f"Estimated cost: ~${low:,.2f} - ${high:,.2f}  (Sonnet 4.6 vision via batches)")

    if images_to_enrich == 0:
        return
    if mode in ("claude-cli", "api"):
        estimate = _sample_runtime_estimate(
            survivors, mode, api_model, cli_model, claude_bin, ocr_kwargs,
        )
        if estimate:
            low_s, high_s = estimate
            transport = "claude-cli" if mode == "claude-cli" else "API"
            print(f"Estimated time: ~{_fmt_duration(low_s)} - {_fmt_duration(high_s)}  "
                  f"(Sonnet 4.6 vision via {transport}; from a 5-PDF sample)")
        else:
            print("Estimated time: unavailable (sample produced no usable timings).")
    elif mode == "batches":
        print("Estimated time: ~1h - 24h  (Anthropic Message Batches API; "
              "typically completes within an hour, server-side parallel)")
    elif mode == "command":
        print("Estimated time: depends on when you run the emitted commands.")
    else:
        print("Estimated time: pass --fallback {claude-cli|api|batches} to get a runtime estimate.")


# === CLI ===
# argparse plumbing and the top-level run loop.

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert PDFs to LLM-friendly Markdown, with optional triage + "
                    "Claude fallback and per-image enrichment.",
    )
    parser.add_argument("root_dir", nargs="?",
                        help="Root directory to search for PDFs (optional with --check)")
    parser.add_argument(
        "--force", action="store_true",
        help="Re-convert even if .md file already exists",
    )
    parser.add_argument(
        "--jobs", type=int, default=1,
        help="Number of parallel worker processes for PDF conversion "
             "(default: 1). Triage, fallback, and enrich stages stay sequential.",
    )

    parser.add_argument("--ocr", choices=["auto", "always", "never"], default="auto",
                        help="OCR for image-only pages via pymupdf4llm/Tesseract "
                             "(auto = use_ocr, always = force_ocr, never = disable)")
    parser.add_argument("--ocr-dpi", type=int, default=150,
                        help="DPI for OCR rasterization (default: 150)")
    parser.add_argument("--ocr-lang", default="eng",
                        help="Tesseract language (default: eng; e.g. 'eng+deu')")

    parser.add_argument("--triage", action="store_true",
                        help="After each conversion, flag PDFs that likely need better extraction")
    parser.add_argument("--triage-only", action="store_true",
                        help="Skip conversion; only scan existing .md files and flag problems")
    parser.add_argument("--min-chars-per-page", type=int, default=100,
                        help="Below this chars/page in the .md, output is considered minimal "
                             "(default: 100)")

    parser.add_argument("--enrich-figures", action="store_true",
                        help="Transcribe embedded images (tables/formulas/charts) via Claude; "
                             "requires --fallback to choose a transport")
    parser.add_argument("--enrich-max-images-per-pdf", type=int, default=0,
                        metavar="N",
                        help="Skip PDFs with more than N image placeholders "
                             "(0 = no cap, default).")
    parser.add_argument("--enrich-min-image-pixels", nargs=2, type=int,
                        default=[0, 0], metavar=("W", "H"),
                        help="Drop image placeholders smaller than W x H "
                             "(default: 0 0 = no filter).")
    parser.add_argument("--enrich-skip-pdfs", default=None, metavar="FILE",
                        help="Plain-text file of substrings (one per line). "
                             "PDFs whose path matches any line are skipped entirely. "
                             "Lines starting with '#' are comments.")
    parser.add_argument("--enrich-dry-run", action="store_true",
                        help="Iterate the library, print projected counts and a cost "
                             "estimate, then exit. No re-extraction, no Claude calls.")
    parser.add_argument("--enrich-keep-images", action="store_true",
                        help="Preserve extracted image files alongside the .md and append a "
                             "*Source figure:* footer to each enriched slot. Aligns with "
                             "vision-LLM SecondBrain workflows where the original figure may "
                             "be viewed for additional context after reading the text.")
    parser.add_argument("--enrich-restore-broken", action="store_true",
                        help="Standalone repair: find IMAGE_REFs in every .md whose target "
                             "file no longer exists and replace them with [unknown] placeholders "
                             "so a subsequent --enrich-figures pass can retry them. No PDF "
                             "re-conversion, no Claude calls.")

    parser.add_argument("--enrich-rate-limit-wait", type=int, default=3600,
                        metavar="SECONDS",
                        help="Default sleep when a rate-limit error fires and no Retry-After "
                             "hint is parseable from the error (default: 3600 = 1 hour).")
    parser.add_argument("--enrich-max-wait", type=int, default=14400,
                        metavar="SECONDS",
                        help="Run-wide cap on total time spent sleeping for quota recovery "
                             "(default: 14400 = 4 hours). Once exceeded, the run stops cleanly "
                             "with the .md files in a retriable state.")
    parser.add_argument("--enrich-quota-threshold", type=int, default=0,
                        metavar="PCT",
                        help="API mode only: when anthropic-ratelimit-* headers report at least "
                             "PCT%% used, sleep until reset before the next call. "
                             "(1-99; 0 = disabled, default).")
    parser.add_argument("--enrich-pace-aware", action="store_true",
                        help="API mode only, on top of --enrich-quota-threshold: extrapolate the "
                             "slope of usage over the last two API calls and sleep proactively "
                             "if the threshold is projected to be crossed within the next 5 calls.")

    parser.add_argument("--fallback",
                        choices=["api", "claude-cli", "command", "batches", "none"],
                        default="none",
                        help="How to handle flagged PDFs / enrich images (default: none)")
    parser.add_argument("--api-model", default="claude-sonnet-4-6",
                        help="Model for --fallback api / batches (default: claude-sonnet-4-6)")
    parser.add_argument("--cli-model", default="claude-sonnet-4-6",
                        help="Model for --fallback claude-cli / command (default: claude-sonnet-4-6)")
    parser.add_argument("--claude-bin", default=None,
                        help="Path to claude CLI (default: auto-detect)")
    parser.add_argument("--command-file", default=None,
                        help="Path for --fallback command output file "
                             "(default: <project>/output/pdf2md_triage_commands_<libhash>.txt)")
    parser.add_argument("--state-dir", default=None,
                        help="Directory for batch state JSON "
                             "(default: <project>/state/)")
    parser.add_argument("--resume-batch", action="store_true",
                        help="Poll the last submitted batch and write results (no conversion/triage)")
    parser.add_argument("--no-triage-cache", action="store_true",
                        help="Force a fresh triage scan; bypass the per-library cache.")
    parser.add_argument("--check", action="store_true",
                        help="Verify dependencies (Tesseract, claude CLI, anthropic) and exit")

    # Tag online-only flags in the help text so `--help` users see the
    # local-vs-online split at a glance.
    for action in parser._actions:
        if not action.help or action.help.startswith("[ONLINE]"):
            continue
        attr = action.dest
        if attr in ONLINE_FLAGS:
            action.help = "[ONLINE] " + action.help
    return parser


# === Validation ===
# Catch flag combinations that are silently ignored or contradictory in the
# current code and either error out or warn so the user knows their command
# was understood as written.

def _validate_args(args: argparse.Namespace) -> None:
    errors: list[str] = []
    warnings: list[str] = []

    # Standalone-only modes that should not be paired with processing flags.
    processing_flags_set = (
        args.force or args.jobs > 1 or args.triage or args.triage_only
        or args.enrich_figures or args.enrich_dry_run
    )
    if args.resume_batch and processing_flags_set:
        errors.append(
            "--resume-batch runs alone (no conversion / triage / enrichment). "
            "Re-run with only --resume-batch and --root_dir.")
    if args.enrich_restore_broken and processing_flags_set:
        errors.append(
            "--enrich-restore-broken is a recovery-only mode. "
            "Re-run with only --enrich-restore-broken and --root_dir, then "
            "re-run --enrich-figures separately.")
    if args.resume_batch and args.enrich_restore_broken:
        errors.append(
            "Pick one of --resume-batch / --enrich-restore-broken; both "
            "short-circuit the run independently.")

    # Models that don't apply to the chosen transport.
    if args.api_model != "claude-sonnet-4-6" and args.fallback in (
            "claude-cli", "command", "none"):
        warnings.append(
            f"--api-model is ignored with --fallback {args.fallback} "
            f"(applies to api / batches only).")
    if args.cli_model != "claude-sonnet-4-6" and args.fallback in (
            "api", "batches", "none"):
        warnings.append(
            f"--cli-model is ignored with --fallback {args.fallback} "
            f"(applies to claude-cli / command only).")

    # --triage-only is conversion-skipping; conversion-side flags don't apply.
    if args.triage_only and args.force:
        warnings.append("--force has no effect with --triage-only (no conversion runs).")
    if args.triage_only and args.jobs > 1:
        warnings.append("--jobs has no effect with --triage-only (no conversion runs).")

    # --enrich-quota-threshold range check (kept for completeness).
    if args.enrich_quota_threshold and (
            args.enrich_quota_threshold < 1 or args.enrich_quota_threshold > 99):
        errors.append("--enrich-quota-threshold must be 1-99 (or 0 to disable).")

    # Surfaced as warnings, not errors.
    for line in warnings:
        print(f"Warning: {line}")
    if errors:
        for line in errors:
            print(f"Error: {line}")
        sys.exit(2)


def _run_check() -> None:
    print("Checking dependencies...\n")
    print("=== Local-only capabilities (work offline) ===")
    print("These cover conversion, OCR, triage, enrichment filters, "
          "dry-runs, and recovery.")
    tess = shutil.which("tesseract")
    if tess:
        print(f"  [OK]   Tesseract OCR             : {tess}")
    else:
        print("  [MISS] Tesseract OCR             : not on PATH "
              "(scanned PDFs will produce empty .md files)")
    try:
        import pymupdf4llm  # noqa: F401
        print("  [OK]   pymupdf4llm package       : installed")
    except ImportError:
        print("  [MISS] pymupdf4llm package       : missing  "
              "(pip install -r requirements.txt)")

    print("\n=== Online capabilities (need internet + Claude access) ===")
    print("Required only if you use --fallback {api,batches,claude-cli} "
          "or --enrich-figures.")
    print("\nFor --fallback claude-cli  (uses your Claude Pro / Max subscription):")
    try:
        resolved = resolve_claude_executable(verify=True)
        print(f"  [OK]   Claude Code CLI           : {resolved}")
    except RuntimeError as exc:
        print("  [MISS] Claude Code CLI           : not available")
        for line in str(exc).splitlines():
            print(f"         {line}")

    print("\nFor --fallback api / batches  (uses Anthropic API, billed per token):")
    try:
        import anthropic  # noqa: F401
        print("  [OK]   anthropic package         : installed")
    except ImportError:
        print("  [MISS] anthropic package         : missing  "
              "(pip install anthropic)")
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("  [OK]   ANTHROPIC_API_KEY         : set")
    else:
        print("  [n/a]  ANTHROPIC_API_KEY         : not set "
              "(needed only for --fallback api / batches)")


def main() -> None:
    # Windows cp1252 stdout crashes on unicode chars in error messages.
    # Force line-buffering so progress shows up when piped or backgrounded.
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    except AttributeError:
        pass

    parser = _build_arg_parser()
    args = parser.parse_args()

    if args.check:
        _run_check()
        return

    state_dir = Path(args.state_dir) if args.state_dir else STATE_DIR_DEFAULT
    output_dir = OUTPUT_DIR_DEFAULT

    if args.enrich_dry_run and not args.enrich_figures:
        print("Error: --enrich-dry-run requires --enrich-figures.")
        sys.exit(1)
    if (args.enrich_figures and args.fallback == "none"
            and not args.enrich_dry_run and not args.enrich_restore_broken):
        print("Error: --enrich-figures requires --fallback (api | claude-cli | batches | command), "
              "or pair with --enrich-dry-run for an estimate-only run.")
        sys.exit(1)

    _validate_args(args)

    root = Path(args.root_dir) if args.root_dir else None
    if root is None or not root.is_dir():
        print(f"Error: '{args.root_dir}' is not a valid directory.")
        sys.exit(1)

    if args.enrich_restore_broken:
        print(f"=== Restoring broken image refs in {root} ===")
        files_updated, refs_restored = restore_broken_image_refs(root)
        print(f"\nRestored {refs_restored} ref(s) across {files_updated} .md file(s).")
        if refs_restored:
            print("Re-run with --enrich-figures to retry the [unknown] placeholders.")
        return

    if args.resume_batch:
        resume_batch(root, state_dir)
        return

    enrich_filters = {
        "max_per_pdf": args.enrich_max_images_per_pdf,
        "min_w": args.enrich_min_image_pixels[0],
        "min_h": args.enrich_min_image_pixels[1],
        "skip_substrings": [],
    }
    if args.enrich_skip_pdfs:
        skip_path = Path(args.enrich_skip_pdfs)
        if not skip_path.is_file():
            print(f"Error: --enrich-skip-pdfs file not found: {skip_path}")
            sys.exit(1)
        enrich_filters["skip_substrings"] = [
            line.strip() for line in skip_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        print(f"Loaded {len(enrich_filters['skip_substrings'])} skip-list entries "
              f"from {skip_path}")

    if args.ocr != "never":
        tess = shutil.which("tesseract")
        if tess:
            print(f"Tesseract OCR: {tess}  (mode={args.ocr}, dpi={args.ocr_dpi}, "
                  f"lang={args.ocr_lang})")
        else:
            print("Warning: Tesseract not on PATH — pymupdf4llm OCR will be a no-op.")
            print("         Use --ocr never to silence this, or install Tesseract:")
            for line in TESSERACT_INSTALL_HINT.splitlines():
                print(f"         {line}")
    ocr_kwargs = _ocr_kwargs(args.ocr, args.ocr_dpi, args.ocr_lang)

    pdf_files = sorted(root.rglob("*.pdf"))
    print(f"Found {len(pdf_files)} PDF(s) in '{root}'")
    if not pdf_files:
        return

    claude_bin = args.claude_bin
    if args.fallback == "claude-cli":
        claude_bin = preflight_claude_cli(claude_bin)

    if args.enrich_dry_run:
        enrich_figures_dry_run(
            pdf_files, root, enrich_filters,
            mode=args.fallback,
            api_model=args.api_model,
            cli_model=args.cli_model,
            claude_bin=claude_bin or "claude",
            ocr_kwargs=ocr_kwargs,
        )
        return

    # Threshold/pace flags only make sense for --fallback api.
    if args.enrich_quota_threshold and args.fallback != "api":
        print("NOTE: --enrich-quota-threshold applies only to --fallback api. "
              f"Running with --fallback {args.fallback}, the proactive threshold "
              "is ignored; reactive auto-resume on rate-limit errors still applies.")
    if args.enrich_pace_aware and args.fallback != "api":
        print("NOTE: --enrich-pace-aware applies only to --fallback api; ignored.")

    command_sink = None
    if args.fallback == "command":
        if args.command_file:
            cmd_path = Path(args.command_file)
        else:
            output_dir.mkdir(parents=True, exist_ok=True)
            cmd_path = triage_command_path(root, output_dir)
        cmd_path.parent.mkdir(parents=True, exist_ok=True)
        command_sink = open(cmd_path, "w", encoding="utf-8")
        command_sink.write("# Run these commands to re-process flagged PDFs / enrich images via Claude Code.\n\n")
        print(f"Writing fallback commands to: {cmd_path}")

    batch_state = {"requests": [], "mapping": {}}
    image_root = root / IMAGE_ROOT_NAME

    rate_limit_tracker = RateLimitTracker(
        max_wait_seconds=float(args.enrich_max_wait),
        rate_limit_wait_seconds=float(args.enrich_rate_limit_wait),
    )

    # Triage cache — loaded lazily, persisted once at end of Pass B.
    triage_cache: dict | None = None
    triage_cache_file: Path | None = None
    if (args.triage or args.triage_only) and not args.no_triage_cache:
        triage_cache_file = triage_cache_path(root, state_dir)
        triage_cache = load_triage_cache(triage_cache_file)
        if triage_cache:
            print(f"Triage cache: {len(triage_cache)} entries loaded from "
                  f"{triage_cache_file}")
        else:
            print(f"Triage cache: empty (will populate {triage_cache_file})")

    success = skipped = failed = flagged = fallback_ok = fallback_fail = 0
    enrich_mds = enrich_imgs = enrich_fail = 0
    enrich_pdfs_skipped = enrich_imgs_dropped = 0
    flagged_report: list[tuple[Path, list[str], bool]] = []
    convert_failures: set[str] = set()
    quota_stopped = False

    try:
        # Pass A: convert
        if not args.triage_only:
            convert_tasks = []
            for pdf_path in pdf_files:
                md_path = pdf_path.with_suffix(".md")
                if md_path.exists() and not args.force:
                    skipped += 1
                else:
                    convert_tasks.append((str(pdf_path), str(md_path)))

            total = len(convert_tasks)
            if total == 0:
                pass
            elif args.jobs > 1:
                print(f"Parallel conversion: jobs={args.jobs}, {total} PDFs to convert "
                      f"(triage/fallback/enrich run sequentially)")
                worker_args = [(p, m, ocr_kwargs) for p, m in convert_tasks]
                start_time = time.time()
                with multiprocessing.Pool(args.jobs) as pool:
                    for i, (pdf_path_str, status, err) in enumerate(
                            pool.imap_unordered(_convert_worker, worker_args), 1):
                        name = Path(pdf_path_str).name
                        suffix = _progress_suffix(start_time, i, total)
                        if status == "ok":
                            print(f"[{i}/{total}] ok: {name}  {suffix}")
                            success += 1
                        else:
                            print(f"[{i}/{total}] ERROR {name}: {err}  {suffix}")
                            failed += 1
                            convert_failures.add(pdf_path_str)
                print(f"Conversion wallclock: {_fmt_duration(time.time() - start_time)}")
            else:
                print(f"Sequential conversion: {total} PDFs to convert")
                start_time = time.time()
                for i, (pdf_path_str, md_path_str) in enumerate(convert_tasks, 1):
                    pdf_path = Path(pdf_path_str)
                    md_path = Path(md_path_str)
                    suffix = _progress_suffix(start_time, i - 1, total)
                    print(f"[{i}/{total}] Converting: {pdf_path.name}  {suffix}")
                    try:
                        convert_with_pymupdf4llm(pdf_path, md_path, ocr_kwargs)
                        success += 1
                    except Exception as exc:
                        print(f"  ERROR: {exc}")
                        failed += 1
                        convert_failures.add(pdf_path_str)
                print(f"Conversion wallclock: {_fmt_duration(time.time() - start_time)}")

        # Pass B: triage / fallback / enrich
        if args.triage or args.triage_only or args.enrich_figures:
            cap = enrich_filters.get("max_per_pdf") or 0
            skip_substrings = enrich_filters.get("skip_substrings") or []
            triage_requested = args.triage or args.triage_only

            for i, pdf_path in enumerate(pdf_files, 1):
                md_path = pdf_path.with_suffix(".md")
                progress_tag = f"[{i}/{len(pdf_files)}]"

                if str(pdf_path) in convert_failures:
                    continue

                # Cheap pre-check: decide whether enrichment will skip this PDF
                # entirely. Reading the .md and counting placeholders is ~5 ms;
                # opening the PDF for triage is ~500 ms. If we know enrichment
                # will skip and the user didn't ask for triage, the whole PDF
                # is fast-path-skipped here without any pymupdf I/O.
                enrich_skip_reason: str | None = None
                if args.enrich_figures and md_path.exists():
                    if _matches_skip_list(pdf_path, skip_substrings):
                        enrich_skip_reason = "skip-list"
                    elif cap > 0:
                        try:
                            md_for_check = md_path.read_text(
                                encoding="utf-8", errors="ignore")
                            placeholder_count = sum(
                                1 for _ in PLACEHOLDER_RE.finditer(md_for_check))
                            if placeholder_count > cap:
                                enrich_skip_reason = "cap-exceeded"
                        except Exception:
                            pass  # fall through to normal enrichment path

                if enrich_skip_reason and not triage_requested:
                    enrich_pdfs_skipped += 1
                    print(f"{progress_tag} SKIP-ENRICH "
                          f"({enrich_skip_reason}): {pdf_path.name}")
                    continue

                if triage_requested:
                    reasons, should_fallback = triage(
                        pdf_path, md_path, args.min_chars_per_page,
                        cache=triage_cache)
                    if reasons:
                        flagged_report.append((pdf_path, reasons, should_fallback))
                    if should_fallback:
                        flagged += 1
                        print(f"{progress_tag} FLAGGED: {pdf_path.name}")
                        for reason in reasons:
                            print(f"    * {reason}")
                        if args.fallback != "none":
                            try:
                                print(f"    -> fallback={args.fallback}")
                                dispatch_fallback(pdf_path, md_path, args.fallback,
                                                  args.api_model, args.cli_model,
                                                  claude_bin or "claude",
                                                  command_sink, batch_state)
                                fallback_ok += 1
                            except Exception as exc:
                                print(f"    FALLBACK ERROR: {exc}")
                                fallback_fail += 1
                                continue

                if (args.enrich_figures and md_path.exists()
                        and enrich_skip_reason is None):
                    try:
                        processed, failed_imgs, dropped, skip_reason = (
                            enrich_figures_for_pdf(
                                pdf_path, md_path, args.fallback,
                                args.api_model, args.cli_model,
                                claude_bin or "claude",
                                ocr_kwargs, command_sink, batch_state, image_root,
                                enrich_filters,
                                keep_images=args.enrich_keep_images,
                                rate_limit_tracker=rate_limit_tracker,
                                quota_threshold=args.enrich_quota_threshold,
                                pace_aware=args.enrich_pace_aware,
                            )
                        )
                        if skip_reason:
                            enrich_pdfs_skipped += 1
                            print(f"{progress_tag} SKIP-ENRICH ({skip_reason}): {pdf_path.name}")
                        elif processed or failed_imgs:
                            enrich_mds += 1
                            enrich_imgs += processed
                            enrich_fail += failed_imgs
                            enrich_imgs_dropped += dropped
                            extra = f", dropped: {dropped}" if dropped else ""
                            print(f"{progress_tag} ENRICHED: {pdf_path.name} "
                                  f"(images: {processed}, failed: {failed_imgs}{extra})")
                        elif dropped:
                            enrich_imgs_dropped += dropped
                    except QuotaExhaustedError as quota_exc:
                        quota_stopped = True
                        print(f"\n=== {quota_exc} ===")
                        print("Stopping enrichment cleanly. The .md files are in a "
                              "retriable state — re-run later to continue.")
                        break
                    except Exception as exc:
                        print(f"    ENRICH ERROR on {pdf_path.name}: {exc}")
                        enrich_fail += 1
                elif (args.enrich_figures and md_path.exists()
                        and enrich_skip_reason is not None):
                    # Pre-check determined we'd skip; report it now that triage
                    # (if requested) has run. Avoids the redundant function call.
                    enrich_pdfs_skipped += 1
                    print(f"{progress_tag} SKIP-ENRICH "
                          f"({enrich_skip_reason}): {pdf_path.name}")
    finally:
        if command_sink is not None:
            command_sink.close()
        if triage_cache is not None and triage_cache_file is not None:
            try:
                save_triage_cache(triage_cache_file, triage_cache)
                print(f"Triage cache saved: {len(triage_cache)} entries -> "
                      f"{triage_cache_file}")
            except Exception as exc:
                print(f"Warning: could not save triage cache: {exc}")

    if args.fallback == "batches":
        submit_batch(batch_state, root, state_dir)

    print()
    if not args.triage_only:
        print(f"Conversion      -> ok: {success}, skipped: {skipped}, failed: {failed}")
    if args.triage or args.triage_only:
        print(f"Triage          -> flagged (needs fallback): {flagged}")
        if args.fallback not in ("none", "batches"):
            print(f"Fallback        -> ok: {fallback_ok}, failed: {fallback_fail}")
    if args.enrich_figures:
        line = (f"Enrich-figures  -> enriched MDs: {enrich_mds}, "
                f"images processed: {enrich_imgs}, failed: {enrich_fail}")
        if enrich_pdfs_skipped:
            line += f", pdfs-skipped: {enrich_pdfs_skipped}"
        if enrich_imgs_dropped:
            line += f", images-dropped (size): {enrich_imgs_dropped}"
        if rate_limit_tracker.total_slept > 0:
            line += f", slept: {_fmt_duration(rate_limit_tracker.total_slept)}"
        print(line)
        if quota_stopped:
            print("(Run stopped early on quota cap. .md files are retriable.)")

    if flagged_report:
        print("\nAll observations:")
        for pdf, reasons, needs in flagged_report:
            rel = pdf.relative_to(root) if pdf.is_relative_to(root) else pdf
            marker = "!" if needs else " "
            print(f"  {marker} {rel}")
            for reason in reasons:
                print(f"      * {reason}")


if __name__ == "__main__":
    main()
