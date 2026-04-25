"""Convert all PDFs in a directory tree to Markdown files using pymupdf4llm.

Triage + optional Claude fallback cover PDFs that pymupdf4llm can't handle
(broken text extraction, math fonts dropped). A separate --enrich-figures
pass re-extracts images and sends each one to Claude for transcription
(rasterized tables / formulas / charts embedded as pictures).
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
from pathlib import Path

import pymupdf
import pymupdf4llm


# ---------------------------------------------------------------------------
# Triage heuristics
# ---------------------------------------------------------------------------

# Fonts that are *specifically* mathematical typesetting.
# DO NOT include generic Symbol / SymbolMT / SegoeUISymbol — those are used
# for bullets, arrows and checkmarks in ordinary text PDFs and cause massive
# false positives.
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

# If the extracted MD already contains plausible math markup, don't flag
# math fonts — pymupdf4llm did capture something.
LATEX_MARKERS = re.compile(
    r"(\$[^$\n]{2,}\$)|"                              # inline $...$
    r"(\$\$[\s\S]+?\$\$)|"                            # display $$...$$
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

# Placeholder emitted by pymupdf4llm when an image is NOT being written out
# (default behavior with write_images=False). Example:
#   **==> picture [61 x 67] intentionally omitted <==**
PLACEHOLDER_RE = re.compile(
    r"\*\*==>\s*picture\s*\[[^\]]*\]\s*intentionally omitted\s*<==\*\*"
)

# Same placeholder, but capturing W and H as integers — used by the
# filtering layer to estimate cost and drop tiny decorative images
# without re-extracting the PDF.
PLACEHOLDER_DIMS_RE = re.compile(
    r"\*\*==>\s*picture\s*\[\s*(\d+)\s*x\s*(\d+)\s*\]\s*"
    r"intentionally omitted\s*<==\*\*"
)


def _placeholder_dims(md_text: str) -> list[tuple[int, int]]:
    """Pull every (W, H) tuple from placeholders in a .md, in document order."""
    return [(int(w), int(h)) for w, h in PLACEHOLDER_DIMS_RE.findall(md_text)]


# Markdown image ref produced by pymupdf4llm when write_images=True.
IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+\.(?:png|jpg|jpeg))\)", re.IGNORECASE)
_FILENAME_SAFE_RE = re.compile(r"[^\w.\- ]")

# Rough Sonnet 4.6 vision cost range per image, batches discount applied.
# Used only by --enrich-dry-run to print a price estimate; tune in one place.
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

BATCH_STATE_FILENAME = "pdf2md_batch.json"


# ---------------------------------------------------------------------------
# Tesseract OCR preflight
# ---------------------------------------------------------------------------

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
    path = shutil.which("tesseract")
    if not path:
        print(TESSERACT_INSTALL_HINT)
        sys.exit(1)
    return path


# ---------------------------------------------------------------------------
# Detection / triage
# ---------------------------------------------------------------------------

def analyze_pdf(pdf_path: Path) -> dict:
    """Inspect a PDF for math-font presence only — pymupdf4llm itself handles
    the scanned / image-per-page case via OCR."""
    info = {"pages": 0, "math_fonts": set(), "error": None}
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
    except Exception as e:
        info["error"] = str(e)
    return info


def md_has_math_markup(md_text: str) -> bool:
    if LATEX_MARKERS.search(md_text):
        return True
    unicode_math = sum(1 for ch in md_text if ch in UNICODE_MATH_CHARS)
    return unicode_math > 5


def triage(pdf_path: Path, md_path: Path, min_chars_per_page: int) -> tuple:
    """
    Return (reasons, should_fallback).

    Hard triggers (should_fallback=True):
      - md-empty / md-missing / md-minimal
      - math-fonts-dropped

    Informational only (no fallback):
      - math-fonts-present-but-captured
    """
    reasons = []
    should_fallback = False

    pdf_info = analyze_pdf(pdf_path)

    if pdf_info["error"]:
        reasons.append(f"pdf-read-error: {pdf_info['error']}")
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

    return reasons, should_fallback


# ---------------------------------------------------------------------------
# claude CLI resolution (fixes Windows "not recognized" errors)
# ---------------------------------------------------------------------------

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
    for c in candidates:
        if c.exists() and (not verify or _verify_claude_binary(str(c))):
            return str(c)

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
    if claude_bin:
        if not _verify_claude_binary(claude_bin):
            print(f"Error: --claude-bin {claude_bin!r} did not respond to `--version`.")
            print("Make sure the path points to the Claude Code CLI, not the desktop app.")
            sys.exit(1)
        return claude_bin
    try:
        resolved = resolve_claude_executable(verify=True)
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)
    try:
        out = subprocess.run(
            [resolved, "--version"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        version = (out.stdout or out.stderr).strip().splitlines()[0] if out.stdout or out.stderr else "?"
    except Exception:
        version = "?"
    print(f"Claude Code CLI OK: {resolved}  ({version})")
    return resolved


# ---------------------------------------------------------------------------
# Full-PDF fallback handlers
# ---------------------------------------------------------------------------

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


def _quote_for_shell(s: str) -> str:
    if os.name == "nt":
        return '"' + s.replace('"', '\\"') + '"'
    return shlex.quote(s)


def build_claude_cli_command(pdf_path: Path, md_path: Path, model: str,
                             claude_bin: str = "claude") -> str:
    prompt = _build_cli_prompt(pdf_path, md_path)
    return (f"{_quote_for_shell(claude_bin)} --model {model} "
            f"--permission-mode acceptEdits --allowedTools Read Write "
            f"-p {_quote_for_shell(prompt)}")


def _claude_cli_argv(pdf_path: Path, md_path: Path, model: str,
                     claude_bin: str) -> list:
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


# ---------------------------------------------------------------------------
# Batches API — full-PDF path
# ---------------------------------------------------------------------------

def _custom_id_for(pdf_path: Path) -> str:
    h = hashlib.md5(str(pdf_path.resolve()).encode("utf-8")).hexdigest()[:20]
    return f"pdf_{h}"


def _custom_id_for_image(pdf_path: Path, ref: str, idx: int) -> str:
    key = f"{pdf_path.resolve()}::{idx}::{ref}"
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:20]
    return f"img_{h}"


def fallback_batches_collect(pdf_path: Path, md_path: Path, model: str,
                             batch_state: dict) -> None:
    size_mb = pdf_path.stat().st_size / (1024 * 1024)
    if size_mb > 32:
        raise RuntimeError(f"PDF is {size_mb:.1f} MB, exceeds API 32 MB limit")
    cid = _custom_id_for(pdf_path)
    batch_state["requests"].append({
        "custom_id": cid,
        "params": {
            "model": model,
            "max_tokens": 16000,
            "messages": _build_api_messages(pdf_path),
        },
    })
    batch_state["mapping"][cid] = {"kind": "pdf", "md_path": str(md_path)}


def submit_batch(batch_state: dict, root: Path) -> None:
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
        "mapping": batch_state["mapping"],
    }
    state_path = root / BATCH_STATE_FILENAME
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    print(f"\nBatch submitted: {batch.id}")
    print(f"  Requests: {len(batch_state['requests'])}")
    print(f"  State saved to: {state_path}")
    print(f"  Poll for results with:")
    print(f"    python pdf2md.py \"{root}\" --resume-batch")
    print("  Batches typically finish within 1 hour (max 24h, 50% cheaper than sync).")


def resume_batch(root: Path) -> None:
    state_path = root / BATCH_STATE_FILENAME
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
    image_edits: dict[str, list[tuple[str, str]]] = {}  # md_path -> [(ref, replacement), ...]

    for result in client.messages.batches.results(batch_id):
        entry = state["mapping"].get(result.custom_id)
        if entry is None:
            continue

        if result.result.type != "succeeded":
            failed += 1
            print(f"  FAILED {result.custom_id}: {result.result}")
            continue

        text = "".join(
            b.text for b in result.result.message.content if b.type == "text"
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

    # Apply per-image edits in bulk per md file
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


# ---------------------------------------------------------------------------
# --enrich-figures: per-image transcription
# ---------------------------------------------------------------------------

def _ocr_kwargs(ocr_mode: str, ocr_dpi: int, ocr_lang: str) -> dict:
    if ocr_mode == "never":
        return {}
    kw = {"use_ocr": True, "ocr_dpi": ocr_dpi, "ocr_language": ocr_lang}
    if ocr_mode == "always":
        kw["force_ocr"] = True
    return kw


def _build_image_api_messages(image_path: Path) -> list:
    img_b64 = base64.standard_b64encode(image_path.read_bytes()).decode("utf-8")
    ext = image_path.suffix.lower().lstrip(".")
    media_type = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/png")
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


def _enrich_image_api(image_path: Path, model: str) -> str:
    import anthropic
    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=4000,
        messages=_build_image_api_messages(image_path),
    )
    return "".join(b.text for b in message.content if b.type == "text").strip()


def _enrich_image_cli(image_path: Path, model: str, claude_bin: str) -> str:
    # The Read tool cannot resolve filenames containing apostrophes or other
    # shell-hostile characters (e.g. curly apostrophe from a PDF title like
    # "China's …"). Copy to a safe temp name when needed so Claude can read it.
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
    s = str(pdf_path)
    return any(needle and needle in s for needle in skip_substrings)


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
) -> tuple[int, int, int, str | None]:
    """Enrich a single PDF's .md by transcribing every embedded image.

    Returns (images_processed, images_failed, images_dropped_size, skip_reason).
    skip_reason is one of None, "skip-list", "cap-exceeded".
    """
    if not md_path.exists():
        return 0, 0, 0, None
    original_md = md_path.read_text(encoding="utf-8", errors="ignore")
    dims = _placeholder_dims(original_md)
    if not dims:
        return 0, 0, 0, None

    # ---- Filter pass: cheap, no PDF re-extraction needed ----
    if _matches_skip_list(pdf_path, filters.get("skip_substrings") or []):
        return 0, 0, 0, "skip-list"

    cap = filters.get("max_per_pdf") or 0
    if cap > 0 and len(dims) > cap:
        return 0, 0, 0, "cap-exceeded"

    min_w = filters.get("min_w") or 0
    min_h = filters.get("min_h") or 0
    survivor_indices = [
        i for i, (w, h) in enumerate(dims)
        if w >= min_w and h >= min_h
    ]
    dropped = len(dims) - len(survivor_indices)
    if not survivor_indices:
        return 0, 0, dropped, None

    # ---- Now re-extract; only spend time on PDFs that have surviving work ----
    need_persistent = (mode == "command")
    if need_persistent:
        pdf_hash = hashlib.md5(str(pdf_path.resolve()).encode("utf-8")).hexdigest()[:10]
        img_dir = image_root / f"{pdf_path.stem}_{pdf_hash}"
        img_dir.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        img_dir = Path(tempfile.mkdtemp(prefix="pdf2md_imgs_"))
        cleanup = True

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

        # Sanity check: pymupdf4llm is deterministic, so the number of refs
        # in the re-extract should equal the number of placeholders we found.
        # If they diverge, abort filtering for this PDF rather than risk
        # processing the wrong images.
        if len(refs) != len(dims):
            print(f"      WARN: ref count mismatch for {pdf_path.name} "
                  f"(placeholders={len(dims)}, refs={len(refs)}). "
                  f"Processing all refs without size filter.")
            chosen = list(range(len(refs)))
            dropped = 0
        else:
            chosen = survivor_indices

        if mode in ("api", "claude-cli"):
            processed = failed = 0
            new_md = md_with_refs
            total_imgs = len(chosen)
            for idx, ref_idx in enumerate(chosen, 1):
                m = refs[ref_idx]
                ref = m.group(0)
                img_path = Path(m.group(1))
                if not img_path.is_absolute():
                    img_path = (img_dir / img_path.name).resolve()
                t0 = time.time()
                try:
                    if mode == "api":
                        replacement = _enrich_image_api(img_path, api_model)
                    else:
                        replacement = _enrich_image_cli(img_path, cli_model, claude_bin)
                    new_md = new_md.replace(ref, replacement, 1)
                    processed += 1
                    print(f"      [img {idx}/{total_imgs}] {img_path.name} done "
                          f"({_fmt_duration(time.time() - t0)})")
                except Exception as e:
                    print(f"      [img {idx}/{total_imgs}] enrich error on {img_path.name}: {e}")
                    failed += 1
            md_path.write_text(new_md, encoding="utf-8")
            return processed, failed, dropped, None

        if mode == "batches":
            md_path.write_text(md_with_refs, encoding="utf-8")
            queued = 0
            for ref_idx in chosen:
                m = refs[ref_idx]
                ref = m.group(0)
                img_path = Path(m.group(1))
                if not img_path.is_absolute():
                    img_path = (img_dir / img_path.name).resolve()
                cid = _custom_id_for_image(pdf_path, ref, ref_idx)
                batch_state["requests"].append({
                    "custom_id": cid,
                    "params": {
                        "model": api_model,
                        "max_tokens": 4000,
                        "messages": _build_image_api_messages(img_path),
                    },
                })
                batch_state["mapping"][cid] = {
                    "kind": "image",
                    "md_path": str(md_path),
                    "image_ref": ref,
                }
                queued += 1
            return queued, 0, dropped, None

        if mode == "command":
            md_path.write_text(md_with_refs, encoding="utf-8")
            queued = 0
            for ref_idx in chosen:
                m = refs[ref_idx]
                img_path = Path(m.group(1))
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
                queued += 1
            return queued, 0, dropped, None

        raise ValueError(f"Unknown enrich mode: {mode}")
    finally:
        if cleanup:
            shutil.rmtree(img_dir, ignore_errors=True)


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
    extrapolate to the whole survivor set.

    `survivors` is a list of (pdf_path, md_path, survivor_indices) tuples.
    Returns (low_seconds, high_seconds) for the whole run, or None if the
    sample produced no usable timings.

    NOTE: this makes real Claude calls. Used only when --enrich-dry-run is
    paired with a synchronous transport (claude-cli / api).
    """
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
                t0 = time.time()
                try:
                    md_text = pymupdf4llm.to_markdown(
                        str(pdf_path), write_images=True,
                        image_path=str(tmp), image_format="png",
                        **ocr_kwargs,
                    )
                except Exception as e:
                    print(f"      re-extract failed: {e}")
                    continue
                extract_dt = time.time() - t0
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

                t0 = time.time()
                try:
                    if mode == "claude-cli":
                        _enrich_image_cli(img_path, cli_model, claude_bin)
                    elif mode == "api":
                        _enrich_image_api(img_path, api_model)
                    else:
                        return None
                    img_dt = time.time() - t0
                    image_times.append(img_dt)
                    print(f"      image call: {_fmt_duration(img_dt)}")
                except Exception as e:
                    print(f"      image call failed: {e}")
        except Exception as e:
            print(f"      sample error: {e}")

    if not extract_times or not image_times:
        return None

    total_pdfs = sum(1 for s in survivors if s[2])
    total_images = sum(len(s[2]) for s in survivors)

    extract_lo = min(extract_times)
    extract_hi = max(extract_times)
    image_lo = min(image_times)
    image_hi = max(image_times)

    # If we only got one sample, give a ±20% margin instead of a flat range.
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

    # Per-PDF survivor data, kept around for the timing-sample step.
    survivors: list[tuple[Path, Path, list[int]]] = []

    for pdf_path in pdf_files:
        md_path = pdf_path.with_suffix(".md")
        if not md_path.exists():
            continue
        try:
            md = md_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        dims = _placeholder_dims(md)
        if not dims:
            continue
        candidate_pdfs += 1
        total_placeholders += len(dims)

        if _matches_skip_list(pdf_path, skip_subs):
            skipped_skiplist.append((pdf_path, len(dims)))
            images_in_skip_pdfs += len(dims)
            continue

        if cap > 0 and len(dims) > cap:
            skipped_cap.append((pdf_path, len(dims)))
            images_in_cap_pdfs += len(dims)
            continue

        survivor_idx = [i for i, (w, h) in enumerate(dims)
                        if w >= min_w and h >= min_h]
        images_dropped_size += len(dims) - len(survivor_idx)
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
        for p, n in worst:
            try:
                rel = p.relative_to(root)
            except ValueError:
                rel = p
            print(f"      {n:>5} placeholders  {rel}")
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

    # ---------- runtime estimate ----------
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


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def convert_with_pymupdf4llm(pdf_path: Path, md_path: Path, ocr_kwargs: dict) -> None:
    md_text = pymupdf4llm.to_markdown(str(pdf_path), **ocr_kwargs)
    md_path.write_text(md_text, encoding="utf-8")


def _convert_worker(task: tuple) -> tuple:
    """Module-level worker for multiprocessing.Pool (Windows spawn needs
    this to be picklable). Returns (pdf_path_str, status, err_or_none)."""
    pdf_path_str, md_path_str, ocr_kwargs = task
    try:
        md_text = pymupdf4llm.to_markdown(pdf_path_str, **ocr_kwargs)
        Path(md_path_str).write_text(md_text, encoding="utf-8")
        return (pdf_path_str, "ok", None)
    except Exception as e:
        return (pdf_path_str, "error", str(e))


def _fmt_duration(seconds: float) -> str:
    """Render seconds as 'Xs', 'Xm Ys', or 'Xh Ym'."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _progress_suffix(start_time: float, done: int, total: int) -> str:
    """'(elapsed 2m 30s, ETA 5m 10s)' using wallclock avg per task."""
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


def main():
    # Windows cp1252 stdout crashes on unicode chars in error messages.
    # Also force line-buffering so progress shows up when piped / run in background.
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    except AttributeError:
        pass

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

    # OCR (handled by pymupdf4llm itself, via Tesseract)
    parser.add_argument("--ocr", choices=["auto", "always", "never"], default="auto",
                        help="OCR for image-only pages via pymupdf4llm/Tesseract "
                             "(auto = use_ocr, always = force_ocr, never = disable)")
    parser.add_argument("--ocr-dpi", type=int, default=150,
                        help="DPI for OCR rasterization (default: 150)")
    parser.add_argument("--ocr-lang", default="eng",
                        help="Tesseract language (default: eng; e.g. 'eng+deu')")

    # Triage
    parser.add_argument("--triage", action="store_true",
                        help="After each conversion, flag PDFs that likely need better extraction")
    parser.add_argument("--triage-only", action="store_true",
                        help="Skip conversion; only scan existing .md files and flag problems")
    parser.add_argument("--min-chars-per-page", type=int, default=100,
                        help="Below this chars/page in the .md, output is considered minimal (default: 100)")

    # Figure enrichment
    parser.add_argument("--enrich-figures", action="store_true",
                        help="Transcribe embedded images (tables/formulas/charts) via Claude; "
                             "requires --fallback to choose a transport")
    parser.add_argument("--enrich-max-images-per-pdf", type=int, default=0,
                        metavar="N",
                        help="Skip PDFs with more than N image placeholders "
                             "(0 = no cap, default). Useful for skipping textbooks/reports "
                             "with hundreds of decorative images.")
    parser.add_argument("--enrich-min-image-pixels", nargs=2, type=int,
                        default=[0, 0], metavar=("W", "H"),
                        help="Drop image placeholders smaller than W x H "
                             "(default: 0 0 = no filter). E.g. '30 30' drops "
                             "bullets/icons/separators.")
    parser.add_argument("--enrich-skip-pdfs", default=None, metavar="FILE",
                        help="Plain-text file of substrings (one per line). "
                             "PDFs whose path matches any line are skipped entirely. "
                             "Lines starting with '#' are comments.")
    parser.add_argument("--enrich-dry-run", action="store_true",
                        help="Iterate the library, print projected counts and a cost "
                             "estimate, then exit. No re-extraction, no Claude calls.")

    # Fallback
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
                        help="Path for --fallback command output file (default: <root>/pdf2md_triage_commands.txt)")
    parser.add_argument("--resume-batch", action="store_true",
                        help="Poll the last submitted batch and write results (no conversion/triage)")
    parser.add_argument("--check", action="store_true",
                        help="Verify that dependencies (Tesseract, claude CLI, anthropic) are installed and exit")

    args = parser.parse_args()

    # --- Standalone check: verify dependencies ---
    if args.check:
        print("Checking dependencies...\n")
        print("For local extraction (pymupdf4llm OCR — scanned PDFs):")
        tess = shutil.which("tesseract")
        if tess:
            print(f"  [OK]   Tesseract: {tess}")
        else:
            print("  [MISS] Tesseract not on PATH. Install:")
            for line in TESSERACT_INSTALL_HINT.splitlines():
                print(f"         {line}")

        print("\nFor --fallback claude-cli  (uses your Claude Max subscription):")
        try:
            resolved = resolve_claude_executable(verify=True)
            print(f"  [OK]   Claude Code CLI: {resolved}")
        except RuntimeError as e:
            print("  [MISS] Claude Code CLI not available.")
            for line in str(e).splitlines():
                print(f"         {line}")

        print("\nFor --fallback api / batches  (uses Anthropic API, billed per token):")
        try:
            import anthropic  # noqa: F401
            print("  [OK]   anthropic package installed")
        except ImportError:
            print("  [MISS] anthropic package not installed. Install: pip install anthropic")
        if os.environ.get("ANTHROPIC_API_KEY"):
            print("  [OK]   ANTHROPIC_API_KEY is set")
        else:
            print("  [n/a]  ANTHROPIC_API_KEY not set "
                  "(only required if you use --fallback api or batches)")
        return

    if args.enrich_dry_run and not args.enrich_figures:
        print("Error: --enrich-dry-run requires --enrich-figures.")
        sys.exit(1)
    if args.enrich_figures and args.fallback == "none" and not args.enrich_dry_run:
        print("Error: --enrich-figures requires --fallback (api | claude-cli | batches | command), "
              "or pair with --enrich-dry-run for an estimate-only run.")
        sys.exit(1)

    root = Path(args.root_dir) if args.root_dir else None
    if root is None or not root.is_dir():
        print(f"Error: '{args.root_dir}' is not a valid directory.")
        sys.exit(1)

    if args.resume_batch:
        resume_batch(root)
        return

    # Build enrichment filters once; reused by dry-run and the real pass.
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

    # Tesseract preflight (unless OCR is disabled)
    if args.ocr != "never":
        tess = shutil.which("tesseract")
        if tess:
            print(f"Tesseract OCR: {tess}  (mode={args.ocr}, dpi={args.ocr_dpi}, lang={args.ocr_lang})")
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

    # Preflight claude CLI if needed — for the regular --fallback claude-cli
    # path AND for --enrich-dry-run with --fallback claude-cli (timing sample).
    claude_bin = args.claude_bin
    if args.fallback == "claude-cli":
        claude_bin = preflight_claude_cli(claude_bin)

    # Dry-run short-circuit. Now after preflights so the timing sample has a
    # working transport.
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

    command_sink = None
    if args.fallback == "command":
        cmd_path = Path(args.command_file) if args.command_file else root / "pdf2md_triage_commands.txt"
        command_sink = open(cmd_path, "w", encoding="utf-8")
        command_sink.write("# Run these commands to re-process flagged PDFs / enrich images via Claude Code.\n\n")
        print(f"Writing fallback commands to: {cmd_path}")

    batch_state = {"requests": [], "mapping": {}}
    image_root = root / "pdf2md_enrich_images"

    success = skipped = failed = flagged = fallback_ok = fallback_fail = 0
    enrich_mds = enrich_imgs = enrich_fail = 0
    enrich_pdfs_skipped = enrich_imgs_dropped = 0
    flagged_report = []
    convert_failures: set[str] = set()  # pdf_path strs that failed convert

    try:
        # ---------------------------- Pass A: convert ----------------------------
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
                    except Exception as e:
                        print(f"  ERROR: {e}")
                        failed += 1
                        convert_failures.add(pdf_path_str)
                print(f"Conversion wallclock: {_fmt_duration(time.time() - start_time)}")

        # ------------------- Pass B: triage / fallback / enrich ------------------
        if args.triage or args.triage_only or args.enrich_figures:
            for i, pdf_path in enumerate(pdf_files, 1):
                md_path = pdf_path.with_suffix(".md")
                tag = f"[{i}/{len(pdf_files)}]"

                if str(pdf_path) in convert_failures:
                    continue

                # Triage / full-PDF fallback
                if args.triage or args.triage_only:
                    reasons, should_fallback = triage(pdf_path, md_path, args.min_chars_per_page)
                    if reasons:
                        flagged_report.append((pdf_path, reasons, should_fallback))
                    if should_fallback:
                        flagged += 1
                        print(f"{tag} FLAGGED: {pdf_path.name}")
                        for r in reasons:
                            print(f"    * {r}")
                        if args.fallback != "none":
                            try:
                                print(f"    -> fallback={args.fallback}")
                                dispatch_fallback(pdf_path, md_path, args.fallback,
                                                  args.api_model, args.cli_model,
                                                  claude_bin or "claude",
                                                  command_sink, batch_state)
                                fallback_ok += 1
                            except Exception as e:
                                print(f"    FALLBACK ERROR: {e}")
                                fallback_fail += 1
                                continue

                # Figure enrichment
                if args.enrich_figures and md_path.exists():
                    try:
                        processed, failed_imgs, dropped, skip_reason = enrich_figures_for_pdf(
                            pdf_path, md_path, args.fallback,
                            args.api_model, args.cli_model,
                            claude_bin or "claude",
                            ocr_kwargs, command_sink, batch_state, image_root,
                            enrich_filters,
                        )
                        if skip_reason:
                            enrich_pdfs_skipped += 1
                            print(f"{tag} SKIP-ENRICH ({skip_reason}): {pdf_path.name}")
                        elif processed or failed_imgs:
                            enrich_mds += 1
                            enrich_imgs += processed
                            enrich_fail += failed_imgs
                            enrich_imgs_dropped += dropped
                            extra = f", dropped: {dropped}" if dropped else ""
                            print(f"{tag} ENRICHED: {pdf_path.name} "
                                  f"(images: {processed}, failed: {failed_imgs}{extra})")
                        elif dropped:
                            enrich_imgs_dropped += dropped
                    except Exception as e:
                        print(f"    ENRICH ERROR on {pdf_path.name}: {e}")
                        enrich_fail += 1
    finally:
        if command_sink is not None:
            command_sink.close()

    if args.fallback == "batches":
        submit_batch(batch_state, root)

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
        print(line)

    if flagged_report:
        print("\nAll observations:")
        for pdf, reasons, needs in flagged_report:
            rel = pdf.relative_to(root) if pdf.is_relative_to(root) else pdf
            marker = "!" if needs else " "
            print(f"  {marker} {rel}")
            for r in reasons:
                print(f"      * {r}")


if __name__ == "__main__":
    main()
