"""End-to-end offline demo of the whole pipeline — no API key or network needed.

Steps: parse an SRT -> heuristic ASR-error scan -> approve one correction ->
translate in batches with the OfflineDebugProvider -> translation-memory
reuse check -> export Persian SRT.

Run from the project root:
    python examples/run_offline_demo.py

NOTE: the offline provider outputs placeholder Persian text; it only proves
the pipeline mechanics (batching, memory, checkpointing, export). Real
translations require a real API key in the GUI (Settings -> API).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("SST_DATA_DIR", str(Path(tempfile.gettempdir()) / "sst_demo_data"))
import sys

sys.path.insert(0, str(PROJECT_ROOT))

from app.core.batch_processor import BatchProcessor
from app.core.context_manager import ContextManager
from app.core.correction_engine import CorrectionEngine
from app.core.events import ProgressEvent
from app.core.glossary_manager import GlossaryManager
from app.core.subtitle_parser import load_srt_file
from app.core.translation_engine import TranslationEngine
from app.core.translation_memory import MemoryPolicy, TranslationMemory
from app.database.database_manager import DatabaseManager
from app.database.models import GlossaryEntry, ProjectRecord
from app.export.srt_exporter import export_srt
from app.providers import create_provider

SAMPLE_SRT = """1
00:00:01,000 --> 00:00:03,500
Hello everyone and welcome back to the channel.

2
00:00:03,500 --> 00:00:06,000
Today the the we are talking about dependency injection in Python.

3
00:00:06,000 --> 00:00:09,200
This pattern makes testing so much easier, trust me.

4
00:00:09,200 --> 00:00:12,000
Let's dive right into it and see how it works in practice.
"""

data_dir = Path(os.environ["SST_DATA_DIR"])
data_dir.mkdir(parents=True, exist_ok=True)
db_path = data_dir / "demo.db"

if db_path.exists():
    db_path.unlink()
db = DatabaseManager(db_path)
db.migrate()

print("=" * 70)
print("STEP 1 — import and parse the SRT file")
sample_path = data_dir / "demo_source.srt"
sample_path.write_text(SAMPLE_SRT, encoding="utf-8")
doc = load_srt_file(sample_path)
print(f"   loaded {len(doc)} entries, encoding: {doc.encoding}")
print(f"   parse issues: {doc.issue_summary() or 'none'}")

project_id = db.project_create(ProjectRecord(name="demo", srt_path=str(sample_path),
                                             target_language="fa"))
db.entries_replace(project_id, doc.to_records(project_id))

print()
print("STEP 2 — glossary + heuristic ASR-error scan")
glossary = GlossaryManager(db)
glossary.add(GlossaryEntry(
    english_term="dependency injection",
    persian_primary="تزریق وابستگی",
    persian_alternatives=["تزریق کارگیری"],
    explanation="الگوی برنامه‌نویسی برای مدیریت وابستگی‌ها",
    category="programming",
))
print(f"   glossary version: {glossary.version()}")

corrections = CorrectionEngine(db, glossary)
proposals = corrections.scan_heuristics(doc, project_id)
print(f"   heuristic proposals: {len(proposals)}")
for proposal in proposals:
    print(f"   - line {proposal.position}: {proposal.reason} (confidence {proposal.confidence:.2f})")
    print(f"     original : {proposal.original_text!r}")
    print(f"     proposed : {proposal.proposed_text!r}")

print()
print("STEP 3 — approve the stutter correction (as the user would in the review dialog)")
approved = [p for p in proposals if "stutter" in p.reason.lower()]
if approved:
    corrections.approve(doc, db.correction_list(project_id)[0].id)
    db.entry_set_working_text(project_id, approved[0].position,
                              doc.get(approved[0].position).working_text,
                              doc.get(approved[0].position).correction_id)
    print(f"   line {approved[0].position} working_text is now: "
          f"{doc.get(approved[0].position).working_text!r}")
    print(f"   original preserved: {doc.get(approved[0].position).original_text!r}")

print()
print("STEP 4 — batch translation with the offline debug provider")
provider = create_provider("offline")
memory = TranslationMemory(db, MemoryPolicy())
context = ContextManager(prev_lines=3, future_lines=2)
engine = TranslationEngine(provider, glossary, context, target_language="fa")


def listener(event: ProgressEvent) -> None:
    if type(event).__name__ in ("BatchStarted", "BatchFinished", "RunFinished"):
        print(f"   [{type(event).__name__}] {event.message} ({event.done}/{event.total})")


processor = BatchProcessor(
    db=db, engine=engine, memory=memory, context_manager=context, glossary=glossary,
    max_lines=2, max_chars=3500, max_retries=2, retry_base_delay=0.05,
    source_file=str(sample_path), listener=listener,
)
summary = processor.run(project_id, doc)
print(f"   result: translated={summary.translated}, reused={summary.reused}, "
      f"failed={summary.failed}, elapsed={summary.elapsed_seconds:.2f}s")

print()
print("STEP 5 — translation memory: a second run with the same content")
print("   (this demo creates a FRESH import, so the approved correction from")
print("    step 3 is not re-applied; the GUI overlays it automatically on resume)")
doc2 = load_srt_file(sample_path)
project2 = db.project_create(ProjectRecord(name="demo-2", srt_path=str(sample_path)))
db.entries_replace(project2, doc2.to_records(project2))
summary2 = processor.run(project2, doc2)
print(f"   result: translated={summary2.translated}, reused={summary2.reused}, "
      f"failed={summary2.failed}")
print("   -> identical lines are served from translation memory; the line whose")
print("      working text differs (missing the approved correction) is translated again,")

print()
print("STEP 6 — export the Persian SRT")
out_path = PROJECT_ROOT / "examples" / "demo_output.fa.srt"
result = export_srt(doc, out_path, bom=True, use_crlf=True)
print(f"   wrote {result.entries_written} entries to {result.path}")
print(f"   untranslated (kept original): {result.untranslated_positions or 'none'}")

db.close()
print()
print("Demo finished. Open examples/demo_output.fa.srt to inspect the result.")
