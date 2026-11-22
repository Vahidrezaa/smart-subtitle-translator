"""Tests for the SQLite layer: migrations, glossary, memory, projects, entries, corrections."""
from __future__ import annotations

import pytest

from app.database.database_manager import DatabaseError, DuplicateTermError
from app.database.models import (
    CorrectionRecord,
    EntryRecord,
    GlossaryEntry,
    MemoryEntry,
    ProjectRecord,
)


def _entry(position: int, **overrides) -> EntryRecord:
    defaults = dict(
        position=position,
        sequence=position + 1,
        start_ms=position * 2000,
        end_ms=position * 2000 + 1800,
        original_text=f"line {position}",
        working_text=f"line {position}",
    )
    defaults.update(overrides)
    return EntryRecord(**defaults)


class TestMigrations:
    def test_migrate_creates_schema(self, db):
        tables = {
            row["name"]
            for row in db._execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "schema_version",
            "glossary",
            "translation_memory",
            "projects",
            "subtitle_entries",
            "corrections",
        } <= tables
        version = db._execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
        assert version == 1

    def test_migrate_is_idempotent(self, db):
        assert db.migrate() == 1
        assert db.migrate() == 1

    def test_foreign_keys_enforced(self, db):
        with pytest.raises(DatabaseError):
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO subtitle_entries (project_id, position, sequence, start_ms,"
                    " end_ms, original_text, working_text) VALUES (999, 0, 1, 0, 1, 'x', 'x')"
                )


class TestGlossary:
    def test_add_and_get(self, db):
        entry_id = db.glossary_add(
            GlossaryEntry(
                english_term="Dependency Injection",
                persian_primary="تزریق وابستگی",
                persian_alternatives=["تزریق کارگیری"],
                explanation="A pattern where components receive dependencies",
                category="programming",
            )
        )
        fetched = db.glossary_get(entry_id)
        assert fetched is not None
        assert fetched.english_term == "Dependency Injection"
        assert fetched.persian_alternatives == ["تزریق کارگیری"]
        assert fetched.category == "programming"

    def test_duplicate_term_rejected_case_insensitive(self, db):
        db.glossary_add(GlossaryEntry(english_term="framework", persian_primary="چارچوب"))
        with pytest.raises(DuplicateTermError):
            db.glossary_add(GlossaryEntry(english_term="FRAMEWORK", persian_primary="فریمورک"))

    def test_update_and_delete(self, db):
        entry_id = db.glossary_add(GlossaryEntry(english_term="callback", persian_primary="فراخوان بازگشتی"))
        db.glossary_update(entry_id, persian_primary="تابع بازگشتی", notes="common usage")
        fetched = db.glossary_get(entry_id)
        assert fetched.persian_primary == "تابع بازگشتی"
        assert fetched.notes == "common usage"
        db.glossary_delete(entry_id)
        assert db.glossary_get(entry_id) is None
        with pytest.raises(DatabaseError):
            db.glossary_delete(entry_id)

    def test_list_search_and_categories(self, db):
        db.glossary_add(GlossaryEntry(english_term="thread pool", persian_primary="استخر رشته", category="concurrency"))
        db.glossary_add(GlossaryEntry(english_term="deadlock", persian_primary="بن‌بست", category="concurrency"))
        db.glossary_add(GlossaryEntry(english_term="cache", persian_primary="حافظه نهان", category="hardware"))
        assert len(db.glossary_list()) == 3
        assert len(db.glossary_list(category="concurrency")) == 2
        assert len(db.glossary_list(search="dead")) == 1
        assert db.glossary_categories() == ["concurrency", "hardware"]


class TestMemory:
    def test_put_then_update_same_key(self, db):
        db.memory_put(MemoryEntry(source_text="Hello", persian_text="سلام", source_hash="h1", context_hash="c1"))
        db.memory_put(MemoryEntry(source_text="Hello", persian_text="سلام دوست من", source_hash="h1", context_hash="c1", model="m1"))
        assert db.memory_count() == 1
        rows = db.memory_find_by_hashes(["h1"])
        assert rows[0].persian_text == "سلام دوست من"
        assert rows[0].model == "m1"

    def test_same_source_different_context_stored_separately(self, db):
        db.memory_put(MemoryEntry(source_text="Right", persian_text="درست", source_hash="h1", context_hash="c1"))
        db.memory_put(MemoryEntry(source_text="Right", persian_text="راست", source_hash="h1", context_hash="c2"))
        assert db.memory_count() == 2

    def test_touch_increments_use_count(self, db):
        memory_id = db.memory_put(MemoryEntry(source_text="x", persian_text="ایکس", source_hash="hx", context_hash="cx"))
        db.memory_touch(memory_id)
        db.memory_touch(memory_id)
        assert db.memory_find_by_hashes(["hx"])[0].use_count == 2


class TestProjectsAndEntries:
    def test_project_roundtrip(self, db):
        project_id = db.project_create(ProjectRecord(name="demo", srt_path="/x/demo.srt", file_hash="abc"))
        record = db.project_get(project_id)
        assert record.name == "demo"
        assert record.state == "new"
        found = db.project_find_by_file("/x/demo.srt", "abc")
        assert found is not None and found.id == project_id
        assert db.project_find_by_file("/x/demo.srt", "other") is None

    def test_entries_replace_and_load(self, db):
        project_id = db.project_create(ProjectRecord(name="p", srt_path="p.srt"))
        db.entries_replace(project_id, [_entry(0), _entry(1)])
        db.entries_replace(project_id, [_entry(0), _entry(1), _entry(2)])
        loaded = db.entries_load(project_id)
        assert [e.position for e in loaded] == [0, 1, 2]
        assert db.entry_count(project_id) == 3

    def test_entry_translation_persists(self, db):
        project_id = db.project_create(ProjectRecord(name="p", srt_path="p.srt"))
        db.entries_replace(project_id, [_entry(0)])
        db.entry_set_translation(project_id, 0, "سلام", "machine")
        record = db.entries_load(project_id)[0]
        assert record.translated_text == "سلام"
        assert record.status == "machine"
        with pytest.raises(DatabaseError):
            db.entry_set_translation(project_id, 99, "؟", "machine")

    def test_entry_working_text_and_correction_link(self, db):
        project_id = db.project_create(ProjectRecord(name="p", srt_path="p.srt"))
        db.entries_replace(project_id, [_entry(0)])
        db.entry_set_working_text(project_id, 0, "today we talk", 7)
        record = db.entries_load(project_id)[0]
        assert record.working_text == "today we talk"
        assert record.correction_id == 7

    def test_cascade_delete(self, db):
        project_id = db.project_create(ProjectRecord(name="p", srt_path="p.srt"))
        db.entries_replace(project_id, [_entry(0), _entry(1)])
        with db.transaction() as conn:
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        assert db.entries_load(project_id) == []


class TestCorrections:
    def test_add_list_set_status(self, db):
        project_id = db.project_create(ProjectRecord(name="p", srt_path="p.srt"))
        record = CorrectionRecord(
            project_id=project_id,
            position=3,
            original_text="pyhton",
            proposed_text="python",
            confidence=0.8,
            reason="close to glossary term",
            method="heuristic",
        )
        correction_id = db.correction_add(record)
        pending = db.correction_list(project_id, status="proposed")
        assert len(pending) == 1
        assert pending[0].proposed_text == "python"
        db.correction_set_status(correction_id, "approved")
        approved = db.correction_get(correction_id)
        assert approved.status == "approved"
        assert approved.decided_at is not None
        assert db.correction_list(project_id, status="proposed") == []
