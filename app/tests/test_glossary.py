"""Tests for glossary term detection, versioning and the memory reuse policy."""
from __future__ import annotations

from app.core.glossary_manager import GlossaryManager
from app.core.subtitle_model import MemoryStatus
from app.core.translation_memory import MemoryPolicy, MemoryQuery, TranslationMemory
from app.database.models import GlossaryEntry, MemoryEntry


class TestGlossaryManager:
    def _manager(self, db) -> GlossaryManager:
        manager = GlossaryManager(db)
        manager.add(GlossaryEntry(english_term="dependency injection", persian_primary="تزریق وابستگی"))
        manager.add(GlossaryEntry(english_term="Python 3", persian_primary="پایتون ۳"))
        manager.add(GlossaryEntry(english_term="cache", persian_primary="حافظه نهان", category="hardware"))
        return manager

    def test_find_terms_case_insensitive(self, db):
        manager = self._manager(db)
        hits = manager.find_terms("We use Dependency Injection everywhere.")
        assert len(hits) == 1
        assert hits[0].matched_text == "Dependency Injection"
        assert hits[0].entry.persian_primary == "تزریق وابستگی"

    def test_longest_term_wins_no_overlap(self, db):
        manager = self._manager(db)
        hits = manager.find_terms("Python 3 rocks. Plain Python is fine too.")
        # "Python 3" matched; the standalone "Python" inside it must not duplicate,
        # and "Python" alone (not a stored term) produces no hit.
        assert [h.entry.english_term for h in hits] == ["Python 3"]

    def test_word_boundary_no_substring_match(self, db):
        manager = self._manager(db)
        # "cache" is a substring of "caches" / "caching" — word boundary must prevent a hit
        assert manager.find_terms("We are caching results.") == []
        assert len(manager.find_terms("Clear the cache, please.")) == 1

    def test_symbols_in_terms_match(self, db):
        manager = GlossaryManager(db)
        manager.add(GlossaryEntry(english_term="C++", persian_primary="سی‌پلاس‌پلاس"))
        assert len(manager.find_terms("Written in C++ since 1998.")) == 1

    def test_version_bumps_on_change(self, db):
        manager = GlossaryManager(db)
        first = manager.version()
        entry_id = manager.add(GlossaryEntry(english_term="thread pool", persian_primary="استخر رشته"))
        second = manager.version()
        assert first != second
        manager.update(entry_id, notes="updated")
        assert manager.version() != second
        manager.delete(entry_id)
        assert manager.version() != ""

    def test_empty_text(self, db):
        assert self._manager(db).find_terms("") == []


class TestTranslationMemory:
    def _memory(self, db, **policy_overrides) -> TranslationMemory:
        return TranslationMemory(db, MemoryPolicy(**policy_overrides))

    def test_exact_match_reused(self, db):
        memory = self._memory(db)
        memory.record("Welcome back to the channel, everyone.", "به کانال دوباره خوش آمدید.", [""])
        decision = memory.find_reusable(
            [MemoryQuery(position=0, source_text="Welcome back to the channel, everyone.",
                         context_hash="x", prev_norms=[""])]
        )[0]
        # context_hash differs but unambiguous long line: affinity 1.0 for empty-vs-empty? prev [""] vs stored [""] → both join to ""
        assert decision.reuse
        assert decision.persian_text == "به کانال دوباره خوش آمدید."

    def test_punctuation_and_case_differences_still_match(self, db):
        memory = self._memory(db)
        memory.record("Hello, World!", "سلام دنیا!", ["previous line"])
        decision = memory.find_reusable(
            [MemoryQuery(position=1, source_text="hello world",
                         context_hash=TranslationMemory.context_hash_for("hello world", ["previous line"]),
                         prev_norms=["previous line"])]
        )[0]
        assert decision.reuse

    def test_machine_status_requires_strong_context(self, db):
        memory = self._memory(db)
        memory.record(
            "That is an interesting question.",
            "این سوال جالبی است.",
            ["alpha", "beta", "gamma"],
            status=MemoryStatus.MACHINE,
        )
        similar_context = ["alpha", "beta", "delta"]
        decision = memory.find_reusable(
            [MemoryQuery(position=9, source_text="That is an interesting question.",
                         context_hash="different", prev_norms=similar_context)]
        )[0]
        # SequenceMatcher ratio ~0.87 < 0.9 required for machine status
        assert not decision.reuse
        assert "below required" in decision.reason

    def test_approved_status_reused_with_weaker_context(self, db):
        memory = self._memory(db)
        memory.record(
            "That is an interesting question.",
            "این سوال جالبی است.",
            ["alpha", "beta", "gamma"],
            status=MemoryStatus.APPROVED,
        )
        decision = memory.find_reusable(
            [MemoryQuery(position=9, source_text="That is an interesting question.",
                         context_hash="different", prev_norms=["alpha", "beta", "delta"])]
        )[0]
        assert decision.reuse
        assert decision.status is MemoryStatus.APPROVED

    def test_ambiguous_line_requires_exact_context(self, db):
        memory = self._memory(db)
        memory.record("it", "آن", ["first", "second", "third"], status=MemoryStatus.APPROVED)
        # same prev → exact context → reuse
        same_ctx = memory.find_reusable(
            [MemoryQuery(position=5, source_text="it",
                         context_hash=TranslationMemory.context_hash_for("it", ["first", "second", "third"]),
                         prev_norms=["first", "second", "third"])]
        )[0]
        assert same_ctx.reuse
        # different prev → blocked even though approved
        other_ctx = memory.find_reusable(
            [MemoryQuery(position=8, source_text="it", context_hash="other",
                         prev_norms=["fourth", "fifth", "sixth"])]
        )[0]
        assert not other_ctx.reuse
        assert "ambiguous" in other_ctx.reason

    def test_speaker_mismatch_blocks_reuse(self, db):
        memory = self._memory(db)
        memory.record("I will handle it myself.", "خودم انجامش می‌دهم.", ["a", "b"], speaker="Alice")
        decision = memory.find_reusable(
            [MemoryQuery(position=2, source_text="I will handle it myself.",
                         context_hash="c", prev_norms=["a", "b"], speaker="Bob")]
        )[0]
        assert not decision.reuse

    def test_disabled_policy(self, db):
        memory = self._memory(db, reuse_enabled=False)
        memory.record("Hello there.", "سلام.", [""])
        decision = memory.find_reusable(
            [MemoryQuery(position=0, source_text="Hello there.", context_hash="c", prev_norms=[""])]
        )[0]
        assert not decision.reuse

    def test_no_memory_match(self, db):
        memory = self._memory(db)
        decision = memory.find_reusable(
            [MemoryQuery(position=0, source_text="Never seen before.", context_hash="c", prev_norms=[""])]
        )[0]
        assert not decision.reuse
        assert decision.reason == "no memory match"

    def test_record_stores_metadata(self, db):
        memory = self._memory(db)
        memory.record("Testing the glossary version stamp.", "آزمون.", ["prev"],
                      glossary_version="v2:abcd", model="gpt-4o-mini", status=MemoryStatus.CORRECTED)
        rows = db.memory_find_by_hashes([TranslationMemory.source_hash("Testing the glossary version stamp.")])
        assert rows[0].glossary_version == "v2:abcd"
        assert rows[0].model == "gpt-4o-mini"
        assert rows[0].status == "corrected"

    def test_hash_collision_double_check(self, db):
        memory = self._memory(db)
        # Store a row whose source_norm differs from the query text but craft the
        # stored row directly with a forged hash to simulate a collision.
        db.memory_put(MemoryEntry(
            source_text="completely different words here",
            persian_text="جایگزین",
            source_norm="completely different words here",
            source_hash=TranslationMemory.source_hash("Register the dependency first please"),
            context_hash="c",
            context_summary=[],
            status="machine",
        ))
        decision = memory.find_reusable(
            [MemoryQuery(position=0, source_text="Register the dependency first please",
                         context_hash="c", prev_norms=[])]
        )[0]
        assert not decision.reuse  # difflib similarity check rejects the forged match

    def test_use_count_incremented_by_db(self, db):
        memory = self._memory(db)
        memory.record("A clear reusable sentence for counting.", "شمارش.", ["x"])
        decision = memory.find_reusable(
            [MemoryQuery(position=1, source_text="A clear reusable sentence for counting.",
                         context_hash="c", prev_norms=["x"])]
        )[0]
        assert decision.reuse
        db.memory_touch(decision.memory_id)
        rows = db.memory_find_by_hashes([TranslationMemory.source_hash("A clear reusable sentence for counting.")])
        assert rows[0].use_count == 1
