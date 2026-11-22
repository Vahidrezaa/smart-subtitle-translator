"""Thread-safe SQLite access with schema migrations.

One connection shared between the GUI thread and worker threads, guarded by an
RLock — safe for the "GUI reads while a worker writes" pattern this app uses.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from app.database.models import (
    CorrectionRecord,
    EntryRecord,
    GlossaryEntry,
    MemoryEntry,
    ProjectRecord,
)

log = logging.getLogger(__name__)


class DatabaseError(RuntimeError):
    pass


class DatabaseIntegrityError(DatabaseError):
    """Constraint violation (foreign keys, UNIQUE, ...) raised as a DatabaseError."""


class DuplicateTermError(DatabaseIntegrityError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS glossary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    english_term TEXT NOT NULL COLLATE NOCASE UNIQUE,
    persian_primary TEXT NOT NULL,
    persian_alternatives TEXT NOT NULL DEFAULT '[]',
    explanation TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_glossary_category ON glossary(category);

CREATE TABLE IF NOT EXISTS translation_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_text TEXT NOT NULL,
    source_norm TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL,
    persian_text TEXT NOT NULL,
    context_hash TEXT NOT NULL DEFAULT '',
    context_summary TEXT NOT NULL DEFAULT '[]',
    glossary_version TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'machine',
    speaker TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    use_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tm_lookup ON translation_memory(source_hash, status);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    srt_path TEXT NOT NULL,
    file_hash TEXT NOT NULL DEFAULT '',
    target_language TEXT NOT NULL DEFAULT 'fa',
    model TEXT NOT NULL DEFAULT '',
    glossary_version TEXT NOT NULL DEFAULT '',
    next_batch INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subtitle_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    original_text TEXT NOT NULL,
    working_text TEXT NOT NULL,
    translated_text TEXT,
    status TEXT NOT NULL DEFAULT 'untranslated',
    speaker TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'srt',
    correction_id INTEGER,
    UNIQUE(project_id, position)
);
CREATE INDEX IF NOT EXISTS idx_entries_project ON subtitle_entries(project_id, position);

CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    original_text TEXT NOT NULL,
    proposed_text TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    reason TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL DEFAULT 'heuristic',
    status TEXT NOT NULL DEFAULT 'proposed',
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_corr_project ON corrections(project_id, status);
"""

_MIGRATIONS: list[str] = [SCHEMA_V1]

_GLOSSARY_FIELDS = {
    "english_term",
    "persian_primary",
    "persian_alternatives",
    "explanation",
    "category",
    "notes",
}
_PROJECT_FIELDS = {
    "name",
    "target_language",
    "model",
    "glossary_version",
    "next_batch",
    "state",
}


class DatabaseManager:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path) if str(db_path) != ":memory:" else None
        if self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        if self.db_path is not None:
            self._conn.execute("PRAGMA journal_mode=WAL")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise DatabaseIntegrityError(str(exc)) from exc
            except Exception:
                self._conn.rollback()
                raise

    def _execute(self, sql: str, params: Sequence = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    # ------------------------------------------------------------ migrations
    def migrate(self) -> int:
        """Apply pending migrations; returns the current schema version."""
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM schema_version"
            ).fetchone()
            current = row["v"] or 0
            for version in range(current, len(_MIGRATIONS)):
                self._conn.executescript(_MIGRATIONS[version])
                self._conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (version + 1,)
                )
                self._conn.commit()
                log.info("Applied database migration v%d", version + 1)
            return len(_MIGRATIONS)

    # --------------------------------------------------------------- glossary
    def glossary_add(self, entry: GlossaryEntry) -> int:
        now = utc_now()
        try:
            with self.transaction() as conn:
                cur = conn.execute(
                    """INSERT INTO glossary
                       (english_term, persian_primary, persian_alternatives,
                        explanation, category, notes, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry.english_term.strip(),
                        entry.persian_primary.strip(),
                        json.dumps(entry.persian_alternatives, ensure_ascii=False),
                        entry.explanation,
                        entry.category,
                        entry.notes,
                        now,
                        now,
                    ),
                )
                return int(cur.lastrowid)
        except DatabaseIntegrityError as exc:
            raise DuplicateTermError(
                f"Glossary term already exists: {entry.english_term!r}"
            ) from exc

    def glossary_update(self, entry_id: int, **changes) -> None:
        if not changes:
            return
        unknown = set(changes) - _GLOSSARY_FIELDS
        if unknown:
            raise DatabaseError(f"Unknown glossary fields: {sorted(unknown)}")
        if "persian_alternatives" in changes:
            changes["persian_alternatives"] = json.dumps(
                changes["persian_alternatives"], ensure_ascii=False
            )
        changes["updated_at"] = utc_now()
        sets = ", ".join(f"{k} = ?" for k in changes)
        with self.transaction() as conn:
            cur = conn.execute(
                f"UPDATE glossary SET {sets} WHERE id = ?",
                (*changes.values(), entry_id),
            )
            if cur.rowcount == 0:
                raise DatabaseError(f"Glossary entry {entry_id} not found")

    def glossary_delete(self, entry_id: int) -> None:
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM glossary WHERE id = ?", (entry_id,))
            if cur.rowcount == 0:
                raise DatabaseError(f"Glossary entry {entry_id} not found")

    def glossary_get(self, entry_id: int) -> GlossaryEntry | None:
        row = self._execute("SELECT * FROM glossary WHERE id = ?", (entry_id,)).fetchone()
        return self._glossary_from_row(row) if row else None

    def glossary_list(self, search: str = "", category: str = "") -> list[GlossaryEntry]:
        sql = "SELECT * FROM glossary"
        clauses, params = [], []
        if category:
            clauses.append("category = ?")
            params.append(category)
        if search:
            clauses.append(
                "(english_term LIKE ? OR persian_primary LIKE ? OR explanation LIKE ?)"
            )
            like = f"%{search}%"
            params += [like, like, like]
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY english_term COLLATE NOCASE"
        rows = self._execute(sql, params).fetchall()
        return [self._glossary_from_row(r) for r in rows]

    def glossary_categories(self) -> list[str]:
        rows = self._execute(
            "SELECT DISTINCT category FROM glossary WHERE category != '' ORDER BY category"
        ).fetchall()
        return [r["category"] for r in rows]

    def glossary_count(self) -> int:
        return int(
            self._execute("SELECT COUNT(*) AS c FROM glossary").fetchone()["c"]
        )

    @staticmethod
    def _glossary_from_row(row: sqlite3.Row) -> GlossaryEntry:
        try:
            alternatives = json.loads(row["persian_alternatives"])
        except (TypeError, ValueError):
            alternatives = []
        if not isinstance(alternatives, list):
            alternatives = []
        return GlossaryEntry(
            id=int(row["id"]),
            english_term=row["english_term"],
            persian_primary=row["persian_primary"],
            persian_alternatives=alternatives,
            explanation=row["explanation"],
            category=row["category"],
            notes=row["notes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # -------------------------------------------------------- translation memory
    def memory_find_by_hashes(self, hashes: Sequence[str]) -> list[MemoryEntry]:
        if not hashes:
            return []
        placeholders = ",".join("?" * len(hashes))
        rows = self._execute(
            f"SELECT * FROM translation_memory WHERE source_hash IN ({placeholders}) "
            "ORDER BY updated_at DESC",
            list(hashes),
        ).fetchall()
        return [self._memory_from_row(r) for r in rows]

    def memory_put(self, entry: MemoryEntry) -> int:
        """Insert, or update the row with the same (source_hash, context_hash)."""
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM translation_memory WHERE source_hash = ? AND context_hash = ?",
                (entry.source_hash, entry.context_hash),
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE translation_memory
                       SET source_text=?, persian_text=?, context_summary=?,
                           glossary_version=?, model=?, status=?, speaker=?,
                           source_file=?, updated_at=?
                       WHERE id=?""",
                    (
                        entry.source_text,
                        entry.persian_text,
                        json.dumps(entry.context_summary, ensure_ascii=False),
                        entry.glossary_version,
                        entry.model,
                        entry.status,
                        entry.speaker,
                        entry.source_file,
                        now,
                        int(row["id"]),
                    ),
                )
                return int(row["id"])
            cur = conn.execute(
                """INSERT INTO translation_memory
                   (source_text, source_norm, source_hash, persian_text, context_hash,
                    context_summary, glossary_version, model, status, speaker,
                    source_file, created_at, updated_at, use_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    entry.source_text,
                    entry.source_norm,
                    entry.source_hash,
                    entry.persian_text,
                    entry.context_hash,
                    json.dumps(entry.context_summary, ensure_ascii=False),
                    entry.glossary_version,
                    entry.model,
                    entry.status,
                    entry.speaker,
                    entry.source_file,
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def memory_touch(self, memory_id: int) -> None:
        self._execute(
            "UPDATE translation_memory SET use_count = use_count + 1 WHERE id = ?",
            (memory_id,),
        )

    def memory_count(self) -> int:
        return int(
            self._execute("SELECT COUNT(*) AS c FROM translation_memory").fetchone()["c"]
        )

    @staticmethod
    def _memory_from_row(row: sqlite3.Row) -> MemoryEntry:
        try:
            summary = json.loads(row["context_summary"])
        except (TypeError, ValueError):
            summary = []
        if not isinstance(summary, list):
            summary = []
        return MemoryEntry(
            id=int(row["id"]),
            source_text=row["source_text"],
            source_norm=row["source_norm"],
            source_hash=row["source_hash"],
            persian_text=row["persian_text"],
            context_hash=row["context_hash"],
            context_summary=summary,
            glossary_version=row["glossary_version"],
            model=row["model"],
            status=row["status"],
            speaker=row["speaker"],
            source_file=row["source_file"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            use_count=int(row["use_count"]),
        )

    # ---------------------------------------------------------------- projects
    def project_create(self, record: ProjectRecord) -> int:
        now = utc_now()
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO projects
                   (name, srt_path, file_hash, target_language, model,
                    glossary_version, next_batch, state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.name,
                    record.srt_path,
                    record.file_hash,
                    record.target_language,
                    record.model,
                    record.glossary_version,
                    record.next_batch,
                    record.state,
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def project_find_by_file(self, srt_path: str, file_hash: str) -> ProjectRecord | None:
        row = self._execute(
            "SELECT * FROM projects WHERE srt_path = ? AND file_hash = ? ORDER BY id DESC",
            (srt_path, file_hash),
        ).fetchone()
        return self._project_from_row(row) if row else None

    def project_get(self, project_id: int) -> ProjectRecord:
        row = self._execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise DatabaseError(f"Project {project_id} not found")
        return self._project_from_row(row)

    def project_update(self, project_id: int, **changes) -> None:
        unknown = set(changes) - _PROJECT_FIELDS
        if unknown:
            raise DatabaseError(f"Unknown project fields: {sorted(unknown)}")
        if not changes:
            return
        changes["updated_at"] = utc_now()
        sets = ", ".join(f"{k} = ?" for k in changes)
        with self.transaction() as conn:
            cur = conn.execute(
                f"UPDATE projects SET {sets} WHERE id = ?",
                (*changes.values(), project_id),
            )
            if cur.rowcount == 0:
                raise DatabaseError(f"Project {project_id} not found")

    @staticmethod
    def _project_from_row(row: sqlite3.Row) -> ProjectRecord:
        return ProjectRecord(
            id=int(row["id"]),
            name=row["name"],
            srt_path=row["srt_path"],
            file_hash=row["file_hash"],
            target_language=row["target_language"],
            model=row["model"],
            glossary_version=row["glossary_version"],
            next_batch=int(row["next_batch"]),
            state=row["state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ----------------------------------------------------------------- entries
    def entries_replace(self, project_id: int, entries: Sequence[EntryRecord]) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM subtitle_entries WHERE project_id = ?", (project_id,))
            conn.executemany(
                """INSERT INTO subtitle_entries
                   (project_id, position, sequence, start_ms, end_ms, original_text,
                    working_text, translated_text, status, speaker, source, correction_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        project_id,
                        e.position,
                        e.sequence,
                        e.start_ms,
                        e.end_ms,
                        e.original_text,
                        e.working_text,
                        e.translated_text,
                        e.status,
                        e.speaker,
                        e.source,
                        e.correction_id,
                    )
                    for e in entries
                ],
            )

    def entries_load(self, project_id: int) -> list[EntryRecord]:
        rows = self._execute(
            "SELECT * FROM subtitle_entries WHERE project_id = ? ORDER BY position",
            (project_id,),
        ).fetchall()
        return [self._entry_from_row(r) for r in rows]

    def entry_set_translation(
        self, project_id: int, position: int, persian_text: str | None, status: str
    ) -> None:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE subtitle_entries SET translated_text = ?, status = ? "
                "WHERE project_id = ? AND position = ?",
                (persian_text, status, project_id, position),
            )
            if cur.rowcount == 0:
                raise DatabaseError(
                    f"Entry (project {project_id}, position {position}) not found"
                )

    def entry_set_working_text(
        self, project_id: int, position: int, working_text: str, correction_id: int | None
    ) -> None:
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE subtitle_entries SET working_text = ?, correction_id = ? "
                "WHERE project_id = ? AND position = ?",
                (working_text, correction_id, project_id, position),
            )
            if cur.rowcount == 0:
                raise DatabaseError(
                    f"Entry (project {project_id}, position {position}) not found"
                )

    def entry_count(self, project_id: int, status: str | None = None) -> int:
        if status is None:
            sql = "SELECT COUNT(*) AS c FROM subtitle_entries WHERE project_id = ?"
            params: tuple = (project_id,)
        else:
            sql = "SELECT COUNT(*) AS c FROM subtitle_entries WHERE project_id = ? AND status = ?"
            params = (project_id, status)
        return int(self._execute(sql, params).fetchone()["c"])

    @staticmethod
    def _entry_from_row(row: sqlite3.Row) -> EntryRecord:
        return EntryRecord(
            id=int(row["id"]),
            project_id=int(row["project_id"]),
            position=int(row["position"]),
            sequence=int(row["sequence"]),
            start_ms=int(row["start_ms"]),
            end_ms=int(row["end_ms"]),
            original_text=row["original_text"],
            working_text=row["working_text"],
            translated_text=row["translated_text"],
            status=row["status"],
            speaker=row["speaker"],
            source=row["source"],
            correction_id=row["correction_id"],
        )

    # ------------------------------------------------------------- corrections
    def correction_add(self, record: CorrectionRecord) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO corrections
                   (project_id, position, original_text, proposed_text, confidence,
                    reason, method, status, created_at, decided_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.project_id,
                    record.position,
                    record.original_text,
                    record.proposed_text,
                    record.confidence,
                    record.reason,
                    record.method,
                    record.status,
                    record.created_at or utc_now(),
                    record.decided_at,
                ),
            )
            return int(cur.lastrowid)

    def correction_get(self, correction_id: int) -> CorrectionRecord:
        row = self._execute(
            "SELECT * FROM corrections WHERE id = ?", (correction_id,)
        ).fetchone()
        if row is None:
            raise DatabaseError(f"Correction {correction_id} not found")
        return self._correction_from_row(row)

    def correction_list(
        self, project_id: int, status: str | None = None
    ) -> list[CorrectionRecord]:
        if status is None:
            rows = self._execute(
                "SELECT * FROM corrections WHERE project_id = ? ORDER BY position, id",
                (project_id,),
            ).fetchall()
        else:
            rows = self._execute(
                "SELECT * FROM corrections WHERE project_id = ? AND status = ? "
                "ORDER BY position, id",
                (project_id, status),
            ).fetchall()
        return [self._correction_from_row(r) for r in rows]

    def correction_set_status(self, correction_id: int, status: str) -> None:
        decided = utc_now() if status in ("approved", "rejected") else None
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE corrections SET status = ?, decided_at = COALESCE(?, decided_at) "
                "WHERE id = ?",
                (status, decided, correction_id),
            )
            if cur.rowcount == 0:
                raise DatabaseError(f"Correction {correction_id} not found")

    @staticmethod
    def _correction_from_row(row: sqlite3.Row) -> CorrectionRecord:
        return CorrectionRecord(
            id=int(row["id"]),
            project_id=int(row["project_id"]),
            position=int(row["position"]),
            original_text=row["original_text"],
            proposed_text=row["proposed_text"],
            confidence=float(row["confidence"]),
            reason=row["reason"],
            method=row["method"],
            status=row["status"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
        )
