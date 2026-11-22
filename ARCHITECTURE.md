# Smart Subtitle Translator — Architecture

Version 0.1.0 (first iteration). This document is the architectural blueprint of the
application: layering, data flow, database schema, key design decisions and the
extension points reserved for future features.

## 1. Goal

Import an English SRT file, translate it into natural, context-aware Persian using an
LLM API, and export a synchronized Persian SRT — with a persistent glossary,
translation memory, ASR-error correction review, resumable batch processing and a
Persian-friendly RTL GUI on Windows.

## 2. Layering

Strict one-way dependencies. The `core` package has **zero GUI imports**, so the whole
translation pipeline runs headless (CLI, tests, future OCR/game integrations).

```
┌────────────────────────────────────────────────────────────┐
│ gui/        main_window, dialogs, table_model, workers      │
│             (PySide6; owns threading, never called by core) │
└───────────────┬────────────────────────────────────────────┘
                │ calls / receives events
┌───────────────▼────────────────────────────────────────────┐
│ core/                                                       │
│   subtitle_parser   SRT parsing + validation               │
│   subtitle_model    SubtitleEntry / SubtitleDocument       │
│   context_manager   sliding context windows + ctx hashes   │
│   correction_engine ASR-error detection + review workflow  │
│   translation_engine prompt building + response validation │
│   glossary_manager  glossary CRUD + term detection         │
│   translation_memory  context-aware reuse of past work     │
│   batch_processor   batching, retries, checkpoints, resume │
│   events            progress events (core → GUI bridge)    │
└──────┬─────────────────────┬───────────────────────────────┘
       │                     │
┌──────▼──────────┐   ┌──────▼──────────────────────────────┐
│ providers/      │   │ database/                            │
│ base_provider   │   │ database_manager (sqlite3 + migrations)│
│ openai_provider │   │ models.py (row↔dataclass mapping)     │
│ offline_provider│   └──────────────────────────────────────┘
│ (registry)      │
└─────────────────┘
       ▲
┌──────┴──────────┐   ┌──────────────────────────────────────┐
│ export/         │   │ config/                               │
│ srt_exporter    │   │ app_config, settings_store,           │
└─────────────────┘   │ paths, credentials (keyring/env/file) │
                      └──────────────────────────────────────┘
```

Entry point: `app/main.py` (`python -m app.main`). `app/cli`-style headless use is
possible by importing `core` directly — this is the seam the future real-time
capture/OCR pipeline will use.

## 3. Data flow (one translation run)

```
SRT file ──▶ subtitle_parser ──▶ SubtitleDocument ──▶ (optional) correction_engine
                                     │                      │ proposals ▶ SQLite
                                     │                      ▼ approved corrections
                                     │                 working_text (original kept)
                                     ▼
                              batch_processor
                                 │ 1. plan batches (max lines / max chars / gaps)
                                 │ 2. translation_memory lookup per line
                                 │    → reuse when source + context affinity pass policy
                                 │ 3. context_manager builds window (prev+current+future
                                 │    + glossary hits + correction notes)
                                 │ 4. translation_engine → provider (retry/backoff)
                                 │ 5. validate JSON answer, per-line post-checks
                                 │ 6. persist entries + TM + project checkpoint
                                 ▼
                         SubtitleDocument (translated) ──▶ srt_exporter ──▶ Persian SRT
```

Progress flows from `batch_processor` as `events.ProgressEvent` objects; the GUI
worker thread converts them into Qt signals. Cancellation is a cooperative flag
(`threading.Event`) checked between batches and before each retry.

## 4. Database schema (SQLite, schema-versioned)

Single file `subtitle_translator.db`; migrations in `database_manager._MIGRATIONS`.

| table | purpose | notes |
|---|---|---|
| `schema_version` | migration bookkeeping | applied version = row count |
| `glossary` | EN term ↔ preferred FA equivalent | UNIQUE case-insensitive term; alternatives JSON; category/explanation/notes; content signature `vN:hash` stamped into prompts and TM rows |
| `translation_memory` | reusable translations | `source_hash` (normalized text) + `context_hash` (source + previous lines); `status` = new/machine/corrected/approved; model, glossary version, speaker, use_count |
| `projects` | one row per imported file (path + content hash) | resumable state (`next_batch`, `state`), target language, model |
| `subtitle_entries` | per-line state per project | position, original seq/timestamps, original_text (immutable), working_text, translated_text, status, correction link |
| `corrections` | proposed ASR fixes, reviewable | status proposed/approved/rejected, confidence, reason, method (heuristic/llm/manual) |

`original_text` is never modified — corrections live in `working_text`, and the
input file itself is only ever read.

## 5. Key design decisions

* **`sqlite3` stdlib instead of an ORM.** Zero extra dependencies (helps a later
  Windows-7/PySide2 fallback path), simple schema, full control of the
  (hash-based) TM lookups. One connection guarded by an RLock covers the
  GUI-thread + worker-thread pattern.
* **Own SRT parser** (`subtitle_parser.py`) instead of the `srt` PyPI package:
  we need precise, per-block validation reports (missing timing lines, bad
  timestamps, overlapping entries, echo duplicates, cue settings), tolerant
  recovery that keeps sequence numbers and timestamps *exactly*, and encoding
  fallbacks (UTF-8 / UTF-8-BOM required, cp1252 fallback flagged). The parser is
  ~150 lines and fully unit-tested; no parsing behavior is hidden in a library.
* **Provider abstraction.** `TranslationProvider` receives a
  `ProviderRequest` (messages + params + structured payload) and returns raw
  text content. Transport-level errors are typed
  (`ProviderAuthError`, `ProviderRateLimitError` with `retry_after`,
  `ProviderTransientError`, `ProviderBadResponseError`,
  `ProviderConfigError`) so `batch_processor` can retry exactly the right
  class. Providers register in a registry (`app/providers/__init__.py`);
  `OfflineDebugProvider` ships for keyless pipeline testing.
* **Context design.** Each line's TM key is
  `source_hash` + `context_hash = H(normalized current ‖ normalized previous K
  lines)`. Previous lines also carry their *approved Persian translations* into
  the prompt, which is what keeps terminology consistent across batch
  boundaries. Upcoming lines are sent untranslated, purely for disambiguation.
* **TM reuse policy** (`translation_memory.MemoryPolicy`): exact normalized
  source match required; then a context-affinity check with per-status
  thresholds (approved 0.5, corrected 0.8, machine 0.9). Short / pronoun-only
  ("ambiguous") lines require the exact same previous context. Hash collisions
  are double-checked with `difflib` similarity ≥ 0.98. Approximate matches are
  never applied automatically.
* **Corrections are proposals only.** Heuristics (stuttered words, echoes,
  edit-distance to glossary terms) and an optional LLM review pass write rows
  with confidence + reason into `corrections`. Applying requires user approval
  in the review dialog; `original_text` stays intact for audit/rollback.
* **Credentials never touch code or plain settings JSON.** `config/credentials.py`
  resolves the key from (in order) the `SST_API_KEY` env var → the OS keyring
  (Windows Credential Manager via `keyring`) → an opt-in local file. API keys are
  never logged.
* **Resume = state, not memory.** Every batch commit persists entry statuses and
  the project checkpoint, so a restart resumes from the database, not from RAM.
  Identical lines already in the TM (same or other file) are never re-sent to
  the API.
* **Glossary priority is enforced twice**: in the prompt ("MUST use…") and in a
  post-translation check that emits a warning event when a glossary term was
  present in the source but none of its Persian variants appear in the answer.
  It is advisory — no silent text replacement.

## 6. Extension points (future features, deliberately reserved now)

| Future feature | Where it plugs in |
|---|---|
| Real-time game screen capture | New `source` type on `SubtitleEntry` (`source != "srt"`); pipeline already source-agnostic; GUI-independent core |
| OCR-based subtitle recognition | OCR module produces `SubtitleEntry`s → feeds `batch_processor` directly (no SRT parsing needed) |
| Speaker identification | `SubtitleEntry.speaker` + TM `speaker` column + prompt context already carry it; speakers also gate TM reuse |
| Character-specific translation styles | `Settings.translation.style_note` already flows into the system prompt; a `StyleProfile` registry can replace the string later |
| Colored subtitle overlays | Export layer is a separate module; add an ASS/overlay exporter without touching core |
| Multiple providers | Provider registry + typed provider errors already in place (e.g. Anthropic/Gemini/local = one class each) |
| Shared translation databases | `DatabaseManager` is file-path based; TM rows carry model/glossary-version provenance, ready for merging |
| Translation memory for video games | TM schema is content-agnostic (`source_hash` + context); a game-strings importer can reuse `translation_memory` directly |
| More target languages | `core/languages.py` registry (`code → prompt name, RTL flag`); the GUI combo and prompts are generated from it |

## 7. Testing strategy

`pytest`, all under `app/tests/`. Database tests use `tmp_path` databases; provider
tests inject a fake `requests.Session`; the engine/batch tests use a scripted fake
provider (no network). A GUI smoke test runs PySide6 offscreen. `examples/
run_offline_demo.py` exercises the whole pipeline end-to-end (parse → corrections →
translate → export) without an API key.

## 8. Platform notes

* Primary target: **Windows 10/11, Python 3.12, PySide6** — as specified.
* The brief also mentions Windows 7 SP1. Python ≥ 3.9 and current PySide6 cannot run
  there (official installers refuse). The realistic legacy path is documented in the
  README (Python 3.8.10 + PySide2 for the GUI, or headless core-only use); the code
  deliberately avoids post-3.10 syntax to keep that port cheap, but v1 is tested on
  Python 3.12 / Windows 10-11 only.
