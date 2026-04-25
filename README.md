# PDF2MD4LLM

Convert PDFs to LLM-friendly Markdown files using [pymupdf4llm](https://github.com/pymupdf/RAG).

Inspired by [pdf2md_llm](https://github.com/leoneversberg/pdf2md_llm), but uses direct PDF text extraction instead of a vision-language model — no GPU, no API key, no internet required.

## Features

- Recursively finds all PDFs in a directory tree
- Converts each PDF to Markdown (headings, tables, lists) via pymupdf4llm
- **OCR** for scanned / image-only pages via pymupdf4llm's built-in Tesseract support — no cloud call needed
- Saves `.md` files alongside the original PDFs
- Skips already-converted files on re-run (incremental processing); `--force` to re-run
- **Triage** mode flags PDFs where extraction clearly broke (empty/minimal output, dropped math fonts)
- **Fallback** modes re-process flagged PDFs via Claude's native PDF support
- **`--enrich-figures`** surgically transcribes embedded images (rasterized tables, formulas, charts) into the existing `.md`

## Installation

```bash
pip install -r requirements.txt
# anthropic is only needed for --fallback api / batches
```

### Tesseract (for OCR)

pymupdf4llm relies on the Tesseract binary for OCR. Install it once:

| OS       | Command                                                 |
|----------|---------------------------------------------------------|
| Windows  | https://github.com/UB-Mannheim/tesseract/wiki  or  `choco install tesseract-ocr` |
| macOS    | `brew install tesseract`                                |
| Linux    | `sudo apt install tesseract-ocr`                        |

On Windows, add the install dir (default `C:\Program Files\Tesseract-OCR`) to `PATH` and open a new shell. Verify with `tesseract --version`. Use `python pdf2md.py --check` to confirm everything is wired up.

## Usage

### Basic

```bash
# Convert all PDFs
python pdf2md.py /path/to/storage

# Force re-conversion of all PDFs
python pdf2md.py --force /path/to/storage
```

### Parallel conversion (`--jobs`)

Conversion is CPU-bound (especially with OCR). `--jobs N` runs N PDFs in
parallel via `multiprocessing.Pool`. Triage, fallback, and enrichment stay
sequential — they either touch shared state or are rate-limited by Claude.

```bash
# 4 parallel workers
python pdf2md.py /path/to/storage --force --jobs 4
```

The progress line now shows elapsed time and an ETA based on rolling
wallclock average:

```
[312/1424] ok: paper.pdf  (elapsed 4m 12s, ETA 14m 30s)
```

Suggested worker count:

| Hardware                         | `--jobs` |
|----------------------------------|----------|
| 4-core / 16 GB (e.g. i7-1165G7)  | `4`      |
| 2-core / 8 GB                    | `2`      |
| 8+ cores / 32 GB                 | `6-8`    |

Each worker can briefly use 500 MB - 1 GB on a figure-heavy 200-page book; don't oversubscribe RAM.

### Bulk conversion recipe (library-scale)

For a Zotero-sized library (~1000+ PDFs):

```bash
# 1. Fast pass, all PDFs, parallel
python pdf2md.py "...\storage" --force --jobs 4

# 2. Identify what still looks broken (empty / minimal / math-dropped)
python pdf2md.py "...\storage" --triage-only

# 3. Enrich embedded images (tables / formulas / charts as pictures)
python pdf2md.py "...\storage" --enrich-figures --fallback batches --jobs 4
python pdf2md.py "...\storage" --resume-batch
```

### OCR

pymupdf4llm's built-in OCR handles scanned / image-only pages without any
cloud round-trip. Controlled with `--ocr {auto,always,never}` (default `auto`):

- `auto` — Tesseract runs only on image-covered regions pymupdf couldn't extract text from (`use_ocr=True`).
- `always` — force OCR on every page (`force_ocr=True`), slower but robust on glitchy text layers.
- `never` — disable OCR entirely.

Tune with `--ocr-dpi` (default 150) and `--ocr-lang` (default `eng`; e.g. `eng+deu`).

### Triage (detect problem PDFs)

Triage flags PDFs where pymupdf4llm's text extraction clearly failed, so the Claude fallback is only spent where it adds value.

**Hard triggers (cause fallback):**
- `md-empty` / `md-missing` — extraction produced nothing
- `md-minimal` — output is smaller than `--min-chars-per-page` × page count
- `math-fonts-dropped` — TeX/AMS/STIX math fonts are in the PDF **and** the `.md` contains no LaTeX-looking markup (i.e. math was lost)

**Informational (reported, no fallback):**
- `math-fonts-present-but-captured` — math fonts detected but the `.md` already has LaTeX / unicode math

> Generic `Symbol`, `SymbolMT`, and `SegoeUISymbol` fonts are **intentionally ignored** — they're used for bullets and arrows in ordinary text PDFs and caused massive false positives.
>
> **Scanned PDFs are not a triage category anymore** — pymupdf4llm's OCR (see above) handles them locally. If OCR still can't recover anything, `md-empty` / `md-minimal` will fire and route to the fallback.

```bash
# Triage inline during a new conversion run
python pdf2md.py /path/to/storage --triage

# Triage pass over existing .md files, without re-converting
python pdf2md.py /path/to/storage --triage-only
```

### Fallback for flagged PDFs

Pick one handler via `--fallback`:

| Mode         | What happens                                                                      | Needs                  | Cost                    |
|--------------|-----------------------------------------------------------------------------------|------------------------|-------------------------|
| `claude-cli` | Shells out to `claude -p` — uses your Claude Max subscription                     | `claude` CLI           | Max quota, synchronous  |
| `api`        | Re-convert via Anthropic API synchronously                                        | `ANTHROPIC_API_KEY`    | Per-token, immediate    |
| `batches`    | Submits **all** flagged PDFs as one async job via the Message Batches API         | `ANTHROPIC_API_KEY`    | **50% cheaper**, up to 24h turnaround |
| `command`    | Prints a ready-to-run `claude -p` command and writes it to a `.txt` file          | nothing                | —                       |

> **Model: `claude-sonnet-4-6` is the default for every path.**
> PDF → Markdown is a mechanical extraction task (vision + OCR + LaTeX transcription); Sonnet 4.6
> handles it with strong quality while being ~5× cheaper and faster than Opus. Override with
> `--api-model` or `--cli-model` only if you need Opus-level reasoning (rarely here).

```bash
# Claude Code CLI (uses Max subscription; auto-detects claude.exe on Windows)
python pdf2md.py /path/to/storage --triage-only --fallback claude-cli

# Synchronous API path
python pdf2md.py /path/to/storage --triage-only --fallback api

# Batches API — cheapest for backfilling a large library
python pdf2md.py /path/to/storage --triage-only --fallback batches
# ...then later, when the batch finishes (check email / dashboard):
python pdf2md.py /path/to/storage --resume-batch

# Emit commands only — no execution — saved to <root>/pdf2md_triage_commands.txt
python pdf2md.py /path/to/storage --triage-only --fallback command
```

#### How `--fallback batches` works

1. Script iterates over all PDFs, runs triage.
2. Every PDF that clears a hard trigger is added to an in-memory list of batch requests.
3. At the end of the run, **one** `messages.batches.create(...)` call submits the whole list.
4. A state file (`<root>/pdf2md_batch.json`) stores the batch ID + a mapping `custom_id → .md path`.
5. Later, `--resume-batch` retrieves the batch, writes each result to its target `.md`, and archives the state file as `.json.done`.

### Enrich embedded figures

Scientific PDFs often embed tables, formulas, and chart panels as *raster images* instead of text. pymupdf4llm emits a placeholder for those:

```
**==> picture [280 x 180] intentionally omitted <==**
```

`--enrich-figures` re-extracts each PDF with `write_images=True`, hands every image to Claude with an image-specific prompt (table → GFM, formula → LaTeX, chart → 2-3 sentence description), and splices the response back into the existing `.md`. It requires `--fallback` to pick a transport:

```bash
# Synchronous, per image (API)
python pdf2md.py /path/to/storage --enrich-figures --fallback api

# Via Claude Max subscription
python pdf2md.py /path/to/storage --enrich-figures --fallback claude-cli

# Async, 50% cheaper — one request per image
python pdf2md.py /path/to/storage --enrich-figures --fallback batches
python pdf2md.py /path/to/storage --resume-batch

# Emit shell commands only (for manual execution)
python pdf2md.py /path/to/storage --enrich-figures --fallback command
```

Enrichment is idempotent: the placeholder pattern is replaced with real Markdown, so a second run finds nothing to do. Combine freely with `--triage` to cover both "whole PDF broken" and "individual images need transcription" in one pass.

> **Heads up:** enrichment overwrites the existing `.md` (the re-extracted Markdown is character-for-character the same from pymupdf4llm, only the placeholders differ). If you hand-edited a `.md`, copy it aside first.

### Options

- `--jobs N` — parallel workers for conversion (default: 1; triage/fallback/enrich stay sequential)
- `--ocr {auto,always,never}` — pymupdf4llm OCR mode (default: `auto`)
- `--ocr-dpi N` — OCR rasterization DPI (default: 150)
- `--ocr-lang LANG` — Tesseract language (default: `eng`; e.g. `eng+deu`)
- `--min-chars-per-page N` — threshold for "minimal output" (default: 100)
- `--enrich-figures` — transcribe embedded images via Claude (requires `--fallback`)
- `--api-model MODEL` — model for `--fallback api` / `batches` (default: **`claude-sonnet-4-6`**)
- `--cli-model MODEL` — model for `--fallback claude-cli` / `command` (default: **`claude-sonnet-4-6`**)
- `--claude-bin PATH` — explicit path to `claude.exe` / `claude.cmd` (default: auto-detect)
- `--command-file PATH` — where to write `--fallback command` output (default: `<root>/pdf2md_triage_commands.txt`)
- `--resume-batch` — fetch results from a previously submitted batch
- `--check` — verify Tesseract, Claude CLI, and `anthropic` package are available, then exit

## Example Output

```
Tesseract OCR: C:\Program Files\Tesseract-OCR\tesseract.exe  (mode=auto, dpi=150, lang=eng)
Found 1424 PDF(s) in 'C:\Zotero\storage'
Claude Code CLI OK: C:\Users\me\AppData\Roaming\npm\claude.cmd  (1.x.y)
[412/1424] FLAGGED: scanned_thesis.pdf
    * md-minimal (42 chars / 180 pages)
    -> fallback=claude-cli
[789/1424] FLAGGED: algebra_notes.pdf
    * math-fonts-dropped (CMMI10, CMSY10)
    -> fallback=claude-cli
[902/1424] ENRICHED: paper_with_table_images.pdf (images: 4, failed: 0)
...
Conversion      -> ok: 1200, skipped: 221, failed: 3
Triage          -> flagged (needs fallback): 31
Fallback        -> ok: 29, failed: 2
Enrich-figures  -> enriched MDs: 87, images processed: 312, failed: 4
```

## Limitations

- `--fallback api` / `batches` requires `ANTHROPIC_API_KEY` and has a 32 MB per-PDF limit
- `--fallback claude-cli` is subject to your Claude Max rate limits
- Triage heuristics are best-effort — false positives and false negatives are possible near the `--min-chars-per-page` threshold
