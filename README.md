# Smart Subtitle Translator

Translate English SRT subtitles into **natural, context-aware Persian** using an
LLM API — with a persistent technical glossary, translation memory, ASR-error
correction review, resumable batch processing and a Persian-friendly RTL
desktop GUI for Windows.

This is **v0.1.0**, the first iteration of a larger project (game overlay, OCR
capture and speaker identification are future phases — the architecture already
reserves seams for them, see `ARCHITECTURE.md` §6).

---

## Features

| Area | What it does |
|---|---|
| **Import** | SRT parsing with exact timestamp/sequence preservation, UTF-8 & UTF-8-BOM support (UTF-16 tolerated), per-block validation reports, readable table view |
| **Translation** | Meaning-first, spoken-register Persian; tone, idiom, slang and sarcasm handled in context; technical terms kept in English where that is what Persian developers actually say; no invention, no omission |
| **Context** | Sliding window per request: previous lines **with their approved Persian translations**, current batch, upcoming lines, glossary hits, correction notes |
| **Glossary** | Persistent SQLite glossary (term, preferred Persian, alternatives, explanation, category, notes); injected into every prompt with *MUST use* semantics; post-translation check warns on violations |
| **ASR corrections** | Local heuristics (stuttered words, caption echoes, near-misses of glossary terms) + optional LLM review; everything is a *proposal* with confidence and reason; original text is never modified |
| **Translation memory** | Persistent SQLite memory keyed by normalized text + context hash; reuse requires context affinity thresholds per status (machine 0.9 / corrected 0.8 / approved 0.5); ambiguous short lines require the exact previous context |
| **Batching** | Configurable batch size (lines/chars), exponential-backoff retries (rate-limit aware), checkpoint after every batch, resume after restart, "retry failed lines" action |
| **Export** | Valid UTF-8 Persian SRT (BOM optional, CRLF), original timestamps and order preserved, untranslated lines keep their original text and are reported, source file never modified |
| **Security** | API key stored in the Windows Credential Manager via `keyring` (env var and explicit-file fallbacks available), never hardcoded, never logged |
| **Providers** | Any OpenAI-compatible chat-completions endpoint (OpenAI, OpenRouter, Azure gateways, LM Studio, llama.cpp, vLLM, ...) plus a built-in offline debug provider |

## Requirements

* **Windows 10 or 11** (primary target), macOS/Linux also work
* **Python 3.12** recommended (3.10+ supported)
* An API key for any OpenAI-compatible service

### A note on Windows 7 SP1

The project brief lists both "Windows 10/11" and "Windows 7 SP1 64-bit"; these
conflict: Python ≥ 3.9 and current PySide6 do not run on Windows 7. The realistic
legacy options are:

1. **Headless core on Windows 7** — install Python 3.8.10 (the last version that
   supports Win7) and use the core packages (`app.core`, `app.database`,
   `app.providers`, `app.export`) from your own script; the code avoids
   post-3.10 syntax to keep this cheap. The GUI is the only part that needs
   PySide6.
2. **GUI on Windows 7** — would require Python 3.8 + PySide2 and a port of
   `app/gui` imports; not tested, not supported in v0.1.0.

For the full Windows 10/11 experience, use Python 3.12.

## Installation (Windows)

```bat
:: 1. Install Python 3.12 from https://www.python.org/downloads/
::    (tick "Add python.exe to PATH" in the installer)

:: 2. Get the project and install dependencies
cd smart_subtitle_translator
python -m pip install -r requirements.txt

:: 3. Run
python -m app.main
```

There is no build step; the project runs from source. (Optional packaging into a
single `.exe` can be done later with PyInstaller — not required for v1.)

## First run

1. Click **تنظیمات API** (API settings):
   * *سرویس‌دهنده* — keep **OpenAI-compatible** (or choose **Offline (debug)** to
     test the pipeline without a key — it produces placeholder text, not real
     translations).
   * *آدرس پایه* — default `https://api.openai.com/v1`; for OpenRouter use
     `https://openrouter.ai/api/v1`, for LM Studio `http://localhost:1234/v1`, etc.
   * *نام مدل* — e.g. `gpt-4o-mini`.
   * *کلید API* — paste your key; it is stored in the **Windows Credential
     Manager** (backend "خودکار"). You can also set the `SST_API_KEY`
     environment variable instead.
   * Use **تست اتصال** to verify, then **ذخیره**.
2. Optionally open **واژه‌نامه تخصصی** and add terminology (e.g.
   `dependency injection` → `تزریق وابستگی`). The glossary overrides the model's
   generic wording whenever the term appears.
3. Click **بارگذاری SRT** and pick your English subtitle file. Parse warnings
   (malformed blocks, overlaps, ...) are listed; nothing is silently changed.
4. (Recommended for YouTube auto-captions) Click **بازبینی اصلاحات** →
   **اسکن محلی خطاهای احتمالی**. Review each proposal with its context and
   confidence; **تایید و اعمال** fixes the *working* text only — the original
   transcription stays intact. **بازبینی با مدل** additionally asks the LLM to
   review captions in context (uses API calls).
5. Click **شروع ترجمه**. Progress is checkpointed after every batch — closing
   the app or losing the network mid-run is safe: re-import the same file and
   start again; finished lines are reused, interrupted ones continue. Use
   **توقف** to stop, **تلاش مجدد خطاهای ترجمه** to retry failed lines.
6. You can edit any Persian cell directly in the table — manual edits are marked
   as `corrected` and stored in the translation memory.
7. Click **خروجی SRT فارسی**. The output `<name>.fa.srt` keeps the original
   timestamps, order and sequence numbers, written as UTF-8 (with BOM) with
   CRLF line endings — drop it next to the video and open in PotPlayer (or any
   player). The original file is never touched.

## Configuration files

| Data | Location (Windows) |
|---|---|
| Settings JSON | `%APPDATA%\SmartSubtitleTranslator\settings.json` |
| Database (glossary, memory, projects, corrections) | `%APPDATA%\SmartSubtitleTranslator\subtitle_translator.db` |
| API key | Windows Credential Manager (service `SmartSubtitleTranslator`), or `SST_API_KEY`, or `api_key.txt` only if you explicitly select the file backend |

Portable usage: set the `SST_DATA_DIR` environment variable to any folder to
keep everything together.

## Project layout

```
app/
    main.py                  entry point (python -m app.main)
    gui/                     PySide6: main window, dialogs, table model, workers
    core/
        subtitle_parser.py   tolerant SRT parsing + validation
        subtitle_model.py    entries, document, statuses
        context_manager.py   sliding context windows + context hashes
        translation_engine.py prompt building + response validation
        correction_engine.py ASR-error heuristics + LLM review + approvals
        glossary_manager.py  glossary CRUD + term detection
        translation_memory.py context-aware memory reuse policy
        batch_processor.py   batching, retries, checkpoints, resume
        events.py            core→GUI progress events (Qt-free)
    providers/
        base_provider.py     abstract provider + typed errors
        openai_provider.py   OpenAI-compatible implementation
        offline_provider.py  keyless pipeline-testing provider
    database/
        database_manager.py  thread-safe SQLite + migrations
        models.py            row dataclasses
    export/
        srt_exporter.py      Persian SRT writer
    config/                  settings, paths, credential stores
    tests/                   pytest suite (150+ tests)
examples/run_offline_demo.py end-to-end pipeline demo, no API key needed
ARCHITECTURE.md              the full architectural blueprint
```

## Tests

```bat
python -m pip install -r requirements-dev.txt
python -m pytest
```

The suite covers parsing/validation, database integrity, glossary detection,
memory reuse policy (including ambiguous-line strictness), provider error
mapping over a fake HTTP session, prompt payload shape, batch/retry/resume
semantics (with a scripted fake provider), correction heuristics and the
approve/reject workflow, the exporter, and offscreen GUI smoke tests.
`examples/run_offline_demo.py` runs the entire pipeline (import → corrections →
translate → memory reuse → export) without any API key.

## Honest limitations of v0.1.0

* Translation *quality* is prompt- and model-driven; it was not evaluated
  against a live LLM API during development (the sandbox has no API key). The
  pipeline mechanics around it are fully tested.
* The GUI was verified offscreen (headless rendering) plus automated smoke
  tests — not on a physical Windows desktop session.
* PotPlayer compatibility follows from standard SRT output (UTF-8+BOM, CRLF,
  canonical `HH:MM:SS,mmm`); verify with your player after export.
* Windows 7 SP1: see the note above — Python 3.12/PySide6 do not support it.
