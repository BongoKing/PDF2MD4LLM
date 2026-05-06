# PDF2MD4LLM

Convert PDFs to LLM-friendly Markdown.

[![tests](https://github.com/BongoKing/PDF2MD4LLM/actions/workflows/test.yml/badge.svg)](https://github.com/BongoKing/PDF2MD4LLM/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> Built with Claude Opus 4.7 (Anthropic).

> Most operations are local. The 🌐 marker tags anything that calls Claude or needs `ANTHROPIC_API_KEY` / a Claude Pro/Max subscription. Run `python pdf2md.py --check` to see which local and online capabilities your machine has.

---

## 1. Acknowledgments

This tool stands on a few cornerstone projects:

- [pymupdf4llm](https://github.com/pymupdf/RAG) — text and table extraction, with built-in OCR via Tesseract.
- [Anthropic SDK / API](https://docs.anthropic.com/) — figure enrichment, batches, headers-based pacing.
- [Claude Code](https://github.com/anthropics/claude-code) — the `claude -p` CLI used by `--fallback claude-cli`.
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) — OCR engine pymupdf4llm calls into.
- [pdf2md_llm](https://github.com/leoneversberg/pdf2md_llm) — original inspiration for the workflow.
- Built with [Claude Opus 4.7](https://www.anthropic.com/claude).

---

## 2. Use cases

Convert a literature-management library (Zotero, Mendeley, Papers, or just a folder tree of PDFs) into Markdown so an LLM can read and reason over your full corpus. The Markdown is the storage layer; pdf2md4llm fills it with high-quality text, formulas, and figure descriptions so downstream tools can synthesize across sources.

Two example downstream workflows that pair well with pdf2md4llm:

- **SecondBrain wiki** — Karpathy's [LLM-maintained personal wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) pattern. A vision-capable LLM reads the Markdown first, then pulls up the original figure when needed. Pass `--enrich-keep-images` so figure files survive next to their transcriptions for that lookup.
- **researcher-pack** — Andre Huang's [Claude Code research workflow](https://github.com/andrehuang/researcher-pack) keeps reading, ideation, experiments, and writing connected through a shared plain-text wiki. Its `/paper-read` command ingests papers into that wiki — pdf2md4llm produces the high-fidelity Markdown those papers become.

---

## 3. What you need

| Need | For | Type |
|---|---|---|
| Python 3.10+ | running the script | local |
| `pip install -r requirements.txt` (or `uv sync`) | `pymupdf4llm`, `anthropic` | local |
| Tesseract on PATH | OCR for scanned pages (see below) | local |
| Claude Code CLI | 🌐 `--fallback claude-cli` (Pro/Max subscription) | online |
| `ANTHROPIC_API_KEY` | 🌐 `--fallback api` / `batches`, `--enrich-figures` via API | online |

Run `python pdf2md.py --check` to verify each piece. The output is split into **Local-only capabilities** (work offline) and **Online capabilities** (need internet + Claude access).

### Tesseract install

| OS | Command |
|---|---|
| Windows | https://github.com/UB-Mannheim/tesseract/wiki  or  `choco install tesseract-ocr` |
| macOS | `brew install tesseract` |
| Linux | `sudo apt install tesseract-ocr` |

On Windows, add the install dir (default `C:\Program Files\Tesseract-OCR`) to `PATH` and open a new shell. Verify with `tesseract --version`.

### Optional: uv

A minimal `pyproject.toml` is included for users who prefer [uv](https://docs.astral.sh/uv/):

```bash
uv sync                 # runtime deps
uv sync --extra dev     # + pytest
uv run pdf2md.py ...    # run the script
```

Both flows produce the same outcome.

---

## 4. Recommended workflows

Pick the row matching your goal; run the numbered commands in order. `ROOT` is your library directory (e.g. `"/path/to/Zotero/storage"`).

| # | Goal | Online? | Sequence |
|---|---|---|---|
| 1 | **Convert library — text + OCR only, no LLM** | local | `A1` |
| 2 | **Add new PDFs to an already-converted library** | local | `A1` (incremental: skips existing `.md`s) |
| 3 | **Convert + enrich figures cheaply** | 🌐 online | `A1`, `B0` (dry-run preview), `B1` (submit batch), `B2` (resume) |
| 4 | **Convert + flag broken PDFs + Claude-fix them** | 🌐 online | `A1`, `C1` (triage-only batches), `B2` (resume) |
| 5 | **Recover a library after a failed enrichment run** | local + 🌐 | `D1` (restore broken refs), then row 3 to retry |
| 6 | **Run overnight on a Pro/Max subscription** | 🌐 online | `A1`, `E1` (claude-cli with auto-resume on quota) |
| 7 | **Just check what enrichment would cost** | local | `B0` |
| 8 | **Re-triage to find newly-broken PDFs (incremental)** | local | `F1` (cached; near-instant on a stable library) |

### Command catalogue

```bash
# A1 — Local conversion: text + tables + OCR for scanned pages.
python pdf2md.py ROOT --jobs 4

# B0 — Dry-run cost / time estimate for figure enrichment.
python pdf2md.py ROOT --enrich-figures --enrich-dry-run \
    --enrich-min-image-pixels 30 30 --enrich-max-images-per-pdf 50

# B1 — 🌐 Submit an enrichment batch (50% cheaper, ~1 h turnaround).
python pdf2md.py ROOT --enrich-figures --fallback batches \
    --enrich-min-image-pixels 30 30 --enrich-max-images-per-pdf 50

# B2 — 🌐 Pull batch results once the batch finishes.
python pdf2md.py ROOT --resume-batch

# C1 — 🌐 Triage existing .md files, send broken ones for full Claude re-conversion.
python pdf2md.py ROOT --triage-only --fallback batches

# D1 — Local repair: turn dead image refs back into retriable placeholders.
python pdf2md.py ROOT --enrich-restore-broken

# E1 — 🌐 Overnight-safe per-image enrichment via Claude Pro/Max.
python pdf2md.py ROOT --enrich-figures --fallback claude-cli \
    --enrich-min-image-pixels 30 30 --enrich-max-images-per-pdf 50 \
    --enrich-rate-limit-wait 3600 --enrich-max-wait 14400

# F1 — Local triage audit (cached between runs).
python pdf2md.py ROOT --triage-only
```

`--enrich-rate-limit-wait` and `--enrich-max-wait` make the CLI run safe to start before bed: when quota fires, the script sleeps until reset and retries the same image automatically, capped by `--enrich-max-wait` total seconds. See [§7.2](#72-overnight-safe-quota-handling).

### Always preview cost first

```bash
python pdf2md.py ROOT --enrich-figures --enrich-dry-run \
    --enrich-min-image-pixels 30 30 --enrich-max-images-per-pdf 50
```

Reads existing `.md` files only, prints projected counts and a cost estimate. No Claude calls, no PDF re-extraction. Pair with `--fallback claude-cli` or `--fallback api` to also sample 5 PDFs for a real wallclock estimate.

---

## 5. Incremental updates

When new PDFs land in the library, re-run the same command. The script already skips already-converted files.

```bash
# New PDFs only — existing .md files are kept, no re-extraction.
python pdf2md.py "/path/to/Zotero/storage" --jobs 4

# Enrich figures only on the newly-converted PDFs (older .md files have no
# placeholders left, so they're skipped automatically).
python pdf2md.py "/path/to/Zotero/storage" \
    --enrich-figures --fallback batches \
    --enrich-min-image-pixels 30 30 --enrich-max-images-per-pdf 50
```

Force a re-conversion of everything (e.g., after upgrading pymupdf4llm) with `--force`.

---

## 6. Conversion modes

### 6.1 Parallel conversion (`--jobs`)

Conversion is CPU-bound (especially with OCR). `--jobs N` runs N PDFs in parallel via `multiprocessing.Pool`. Triage, fallback, and enrichment stay sequential — they touch shared state or are rate-limited by Claude.

```bash
python pdf2md.py "/path/to/storage" --force --jobs 4
```

| Hardware | `--jobs` |
|---|---|
| 4-core / 16 GB (e.g. i7-1165G7) | `4` |
| 2-core / 8 GB | `2` |
| 8+ cores / 32 GB | `6-8` |

Each worker can briefly use 500 MB - 1 GB on a figure-heavy 200-page book; don't oversubscribe RAM.

Progress lines include a rolling ETA:

```
[312/1424] ok: paper.pdf  (elapsed 4m 12s, ETA 14m 30s)
```

### 6.2 OCR

`--ocr {auto,always,never}` (default `auto`):

- `auto` — Tesseract runs only on image-covered regions where pymupdf can't extract text (`use_ocr=True`).
- `always` — force OCR on every page (`force_ocr=True`); slower, robust on broken text layers.
- `never` — disable OCR entirely.

Tune with `--ocr-dpi` (default 150) and `--ocr-lang` (default `eng`; e.g. `eng+deu`).

### 6.3 Triage

Triage is a **local-only** audit (no Claude calls) that flags PDFs where pymupdf4llm's text extraction clearly failed, so the 🌐 Claude fallback is only spent where it adds value. Results are cached at `<project>/state/triage_cache_<libhash>.json` keyed on PDF + `.md` mtimes — re-runs after adding a new paper finish in seconds because the unchanged majority is a cache hit. Pass `--no-triage-cache` to force a fresh scan.

| Reason | Hard trigger? |
|---|---|
| `md-empty` / `md-missing` / `md-minimal` (chars/page below threshold) | yes |
| `math-fonts-dropped` (TeX/AMS/STIX fonts in PDF, no LaTeX in `.md`) | yes |
| `math-fonts-present-but-captured` | informational |

Generic `Symbol`, `SymbolMT`, and `SegoeUISymbol` fonts are intentionally ignored — they're used for bullets and arrows in ordinary PDFs and caused massive false positives. Scanned PDFs aren't a triage category anymore — pymupdf4llm's OCR (above) handles them locally; if OCR still produces nothing, `md-empty` / `md-minimal` fire and route to the fallback.

```bash
# Triage inline during a new conversion run
python pdf2md.py /path/to/storage --triage

# Triage pass over existing .md files, no re-extraction
python pdf2md.py /path/to/storage --triage-only
```

### 6.4 🌐 Full-PDF fallback for flagged PDFs

All four transports below are online (Claude calls).

| Mode | What happens | Needs |
|---|---|---|
| `claude-cli` | Shells out to `claude -p` — uses your Pro/Max subscription | `claude` CLI |
| `api` | Re-convert via Anthropic API synchronously | `ANTHROPIC_API_KEY` |
| `batches` | Submits all flagged PDFs as one async job (50% cheaper, ≤24 h turnaround) | `ANTHROPIC_API_KEY` |
| `command` | Prints a ready-to-run `claude -p` command and writes it to a `.txt` file | nothing |

Default model for every path: `claude-sonnet-4-6`. PDF → Markdown is a mechanical extraction task (vision + OCR + LaTeX transcription); Sonnet 4.6 handles it well while being ~5× cheaper and faster than Opus. Override with `--api-model` or `--cli-model`.

```bash
# CLI (auto-detects claude.exe on Windows)
python pdf2md.py /path/to/storage --triage-only --fallback claude-cli

# API
python pdf2md.py /path/to/storage --triage-only --fallback api

# Batches — cheapest for backfilling a library
python pdf2md.py /path/to/storage --triage-only --fallback batches
python pdf2md.py /path/to/storage --resume-batch

# Emit commands only (no execution)
python pdf2md.py /path/to/storage --triage-only --fallback command
```

---

## 7. 🌐 Figure enrichment

The actual `--enrich-figures` call is online (sends each image to Claude). Filters, dry-run, and recovery (§7.1, §7.3) are local.

Scientific PDFs often embed tables, formulas, and chart panels as raster images instead of text. pymupdf4llm emits a placeholder for those:

```
**==> picture [280 x 180] intentionally omitted <==**
```

`--enrich-figures` re-extracts each PDF with `write_images=True`, hands every image to Claude with an image-specific prompt (table → GFM, formula → LaTeX, chart → 2-3 sentence description), and splices the response back into the existing `.md`. It requires `--fallback` to pick a transport, or `--enrich-dry-run` for a preview.

```bash
# Synchronous per-image (API)
python pdf2md.py /path/to/storage --enrich-figures --fallback api

# Via Pro/Max subscription
python pdf2md.py /path/to/storage --enrich-figures --fallback claude-cli

# Async, 50% cheaper — one request per image
python pdf2md.py /path/to/storage --enrich-figures --fallback batches
python pdf2md.py /path/to/storage --resume-batch

# Emit shell commands only (manual execution)
python pdf2md.py /path/to/storage --enrich-figures --fallback command
```

Enrichment is idempotent: the placeholder is replaced with real Markdown, so a second run finds nothing to do. Combine freely with `--triage`.

Enrichment overwrites the existing `.md` (the re-extracted Markdown is character-for-character the same from pymupdf4llm; only the placeholders differ). If you hand-edited a `.md`, copy it aside first.

### 7.1 Filtering enrichment to control cost

A 1000+ PDF library can produce 30,000+ image placeholders, dominated by textbooks and large reports where most "images" are bullets, dividers, or page-number ornaments. Three opt-in filters cut that down:

| Flag | Effect |
|---|---|
| `--enrich-max-images-per-pdf N` | Skip whole PDFs that exceed N placeholders. Surgical for textbooks. |
| `--enrich-min-image-pixels W H` | Drop placeholders below `W × H` (parsed cheaply from `[W x H]`, no PDF re-extraction). |
| `--enrich-skip-pdfs FILE` | Plain-text file, one path-substring per line. `#` lines are comments. |

`--enrich-dry-run` previews the projected counts and cost without making any Claude calls or re-extracting any PDFs:

```bash
# 1. Unfiltered baseline
python pdf2md.py "/path/to/storage" --enrich-figures --enrich-dry-run

# 2. Tune until the numbers look reasonable
python pdf2md.py "/path/to/storage" --enrich-figures --enrich-dry-run \
    --enrich-max-images-per-pdf 50 --enrich-min-image-pixels 30 30

# 3. Run for real with the same filters
python pdf2md.py "/path/to/storage" --enrich-figures --fallback batches \
    --enrich-max-images-per-pdf 50 --enrich-min-image-pixels 30 30
python pdf2md.py "/path/to/storage" --resume-batch
```

Pair `--enrich-dry-run` with `--fallback claude-cli` or `--fallback api` to also sample 5 PDFs for a real wallclock estimate (`~5h 30m - 8h 10m`), drawn from your actual library.

### 7.2 🌐 Overnight-safe quota handling

The figure-enrichment loop is designed to survive Claude's 5-hour rolling rate windows without manual intervention. When a rate-limit / quota error fires inside the per-image loop:

1. Parse `retry-after` (HTTP header, ISO timestamp in stderr, or `anthropic-ratelimit-*-reset`) — sleep that long.
2. If unparseable, sleep `--enrich-rate-limit-wait` (default `3600` = 1 h).
3. Retry the same image. Repeat up to 3 times per image; then restore the placeholder for that slot and move on.
4. Across the whole run, never sleep more than `--enrich-max-wait` (default `14400` = 4 h) total. After the cap, the script stops cleanly with all unprocessed slots restored to placeholders — re-run later to continue.

A rate-limit hit prints clearly:

```
=== Quota hit on Barros_2023.pdf img 7/12 ===
   anthropic-ratelimit-tokens-reset: 2026-04-26T15:42:00Z
   Sleeping 47m until reset, then retrying...
=== Resuming at 2026-04-26T15:43:01Z ===
```

For `--fallback api` only, two extra knobs avoid the limit instead of waiting for it to fire:

- `--enrich-quota-threshold PCT` (1-99). Sleep proactively when usage headers report `≥ PCT` consumption of either requests or tokens. Free — `anthropic-ratelimit-*` headers come back on every API response.
- `--enrich-pace-aware`. On top of the threshold, project the slope of recent usage; if the next 5 calls would cross the threshold, sleep early.

`--fallback claude-cli` ignores both: the CLI doesn't expose `rate_limits` ([anthropics/claude-code#13585](https://github.com/anthropics/claude-code/issues/13585) — statusline-only), and probing via API key could read the wrong quota pool when the CLI is OAuth-authenticated. Reactive auto-resume covers it.

### 7.3 Recovery: restoring broken refs

If an earlier enrichment was interrupted (process killed, quota hit before the slot-rebuild fix landed, etc.) some `.md` files may contain dead `![](...png)` refs pointing at temp directories that no longer exist. Convert them back into retriable placeholders:

```bash
python pdf2md.py "/path/to/storage" --enrich-restore-broken
```

Walks the library, finds image refs whose target file is missing on disk, and replaces each with `**==> picture [unknown] intentionally omitted <==**`. The next `--enrich-figures` run picks them up. Idempotent: a second pass on a clean library is a no-op.

The `[unknown]` token escapes `--enrich-min-image-pixels` (we lost the original dims when the placeholder was overwritten), so restored slots will re-enter the run regardless of the size filter.

### 7.4 Keep original images alongside transcriptions

`--enrich-keep-images` preserves the extracted figure files at `<root>/pdf2md_images/<pdf_stem>_<hash>/` and appends a `*Source figure:* ![](relative-path)` line under each transcription. Useful for the SecondBrain pattern from [§2](#2-use-cases): a vision-capable LLM reads the text first, then views the figure when needed.

---

## 8. Options reference

The **Type** column distinguishes flags that run entirely on your machine (`local`) from flags that call Claude or hit Anthropic's API (`online`, marked 🌐).

### Conversion

| Flag | Default | Type | Effect |
|---|---|---|---|
| `--force` | off | local | Re-convert even if `.md` already exists |
| `--jobs N` | `1` | local | Parallel worker processes for conversion |
| `--ocr {auto,always,never}` | `auto` | local | pymupdf4llm OCR mode |
| `--ocr-dpi N` | `150` | local | OCR rasterization DPI |
| `--ocr-lang LANG` | `eng` | local | Tesseract language (e.g. `eng+deu`) |

### Triage

| Flag | Default | Type | Effect |
|---|---|---|---|
| `--triage` | off | local | Flag PDFs after each conversion (uses cache) |
| `--triage-only` | off | local | Triage existing `.md` files, no re-extraction |
| `--min-chars-per-page N` | `100` | local | Below this chars/page, output is "minimal" |
| `--no-triage-cache` | off | local | Force a fresh triage scan; bypass cache |

### Fallback transports

| Flag | Default | Type | Effect |
|---|---|---|---|
| `--fallback {api,claude-cli,command,batches,none}` | `none` | 🌐 online | How to handle flagged PDFs / enrichment images (`none` and `command` are local) |
| `--api-model MODEL` | `claude-sonnet-4-6` | 🌐 online | Model for `api` / `batches` |
| `--cli-model MODEL` | `claude-sonnet-4-6` | 🌐 online | Model for `claude-cli` / `command` |
| `--claude-bin PATH` | auto-detect | local | Explicit path to `claude.exe` / `claude.cmd` |
| `--command-file PATH` | `<project>/output/pdf2md_triage_commands_<libhash>.txt` | local | Where `--fallback command` writes |
| `--state-dir DIR` | `<project>/state` | local | Where batch state and triage cache JSONs live |
| `--resume-batch` | — | 🌐 online | Poll the last submitted batch and write back |

### Enrichment

| Flag | Default | Type | Effect |
|---|---|---|---|
| `--enrich-figures` | off | 🌐 online | Transcribe embedded images via Claude (requires `--fallback` or `--enrich-dry-run`) |
| `--enrich-max-images-per-pdf N` | `0` (off) | local | Skip PDFs with more than N placeholders |
| `--enrich-min-image-pixels W H` | `0 0` (off) | local | Drop placeholders smaller than W × H |
| `--enrich-skip-pdfs FILE` | none | local | Plain-text file of path-substrings to skip |
| `--enrich-dry-run` | off | local | Count + estimate cost; no API calls |
| `--enrich-keep-images` | off | local | Preserve image files; append `*Source figure:*` footer |
| `--enrich-restore-broken` | — | local | Convert dead image refs back to `[unknown]` placeholders |
| `--enrich-rate-limit-wait SECONDS` | `3600` | 🌐 online | Default sleep when no `retry-after` is parseable |
| `--enrich-max-wait SECONDS` | `14400` | 🌐 online | Cap on total seconds slept across the run |
| `--enrich-quota-threshold PCT` | `0` (off) | 🌐 online | Pre-emptive sleep at this % usage (API only) |
| `--enrich-pace-aware` | off | 🌐 online | Adaptive pacing on top of the threshold (API only) |

### Misc

| Flag | Type | Effect |
|---|---|---|
| `--check` | local | Print local-vs-online dependency report, then exit |

---

## 9. Repository layout

```
pdf2md4llm/
├── pdf2md.py              # entire script: triage, fallback, enrichment, recovery, CLI
├── README.md
├── requirements.txt       # runtime deps (pymupdf4llm, anthropic)
├── requirements-dev.txt   # adds pytest
├── pyproject.toml         # uv / pip-friendly project metadata
└── tests/
    ├── test_enrich_slots.py        # load-bearing regression for the slot-rebuild bug fix
    ├── test_quota.py               # quota / rate-limit detection + retry-after parsing
    ├── test_recovery.py            # --enrich-restore-broken behavior
    ├── test_triage_cache.py        # triage cache key + load/save round-trip + hit-skips-PDF
    ├── test_validation.py          # _validate_args: error/warn for bad flag combinations
    └── test_pass_b_ordering.py     # cap-skip pre-check uses .md only, never opens PDF
```

Run the tests:

```bash
pip install -r requirements-dev.txt   # or: uv sync --extra dev
python -m pytest tests/               # or: uv run pytest
```

---

## 10. Limitations

- `--fallback api` / `batches` requires `ANTHROPIC_API_KEY` and has a 32 MB per-PDF limit.
- `--fallback claude-cli` is subject to your Pro/Max rate limits — pair with `--enrich-rate-limit-wait` and `--enrich-max-wait` for unattended overnight runs.
- Triage heuristics are best-effort — false positives and false negatives are possible near the `--min-chars-per-page` threshold.
- Enrichment overwrites the `.md` for affected PDFs — hand-edits in those files are lost.
- The unit of resumability is the PDF, not the image: a Ctrl-C mid-PDF restores all not-yet-processed slots in that PDF to placeholders on the next run.

---

## License

MIT — see [LICENSE](LICENSE).
