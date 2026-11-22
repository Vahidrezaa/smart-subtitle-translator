"""ASR-error correction engine.

Input subtitle files (especially YouTube automatic captions) contain speech
recognition errors. This engine:

1. runs local heuristics (stuttered words, caption echoes, near-misses of
   glossary terms) over the ORIGINAL text;
2. optionally asks the provider to review captions in context (conservative
   prompt, JSON verdicts with confidence);
3. stores everything as *proposals* in the ``corrections`` table — nothing is
   ever rewritten silently. Applying requires explicit approval, and even then
   only ``working_text`` changes while ``original_text`` is preserved.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from app.core.exceptions import ConfigurationError
from app.core.glossary_manager import GlossaryManager
from app.core.subtitle_model import SubtitleDocument
from app.core.textutils import edit_distance, english_words, normalize_for_match
from app.database.database_manager import DatabaseManager
from app.database.models import CorrectionRecord
from app.providers.base_provider import ChatMessage, ProviderRequest, TranslationProvider

log = logging.getLogger(__name__)

REVIEW_SYSTEM_PROMPT = """You review automatic speech-recognition (ASR) captions of a video for likely transcription errors.

Use the surrounding captions and the technical glossary to decide whether a caption contains a probable ASR error.
Be conservative: propose a correction only when you are quite confident about the intended word or phrase.
Fix only recognition mistakes. Do NOT change grammar, style, or wording otherwise; when in doubt, keep the original text exactly.
Output only JSON, exactly this shape:
{"reviews": [{"i": <line number>, "suspect": true|false, "correction": "<corrected text, or the original text unchanged>", "confidence": 0.0-1.0, "reason": "<short reason>"}]}"""

_COMMON_DOUBLES = {
    "the", "a", "an", "and", "or", "to", "of", "is", "was", "that", "it",
    "in", "on", "for", "i", "you", "we", "they", "he", "she", "be", "do",
}
_STUTTER_RE = re.compile(r"\b(\w+)(?:\s+\1)+\b", re.IGNORECASE)
_DISTANCE_CONFIDENCE = {1: 0.85, 2: 0.65, 3: 0.5}


@dataclass
class CorrectionProposal:
    position: int
    original_text: str
    proposed_text: str
    confidence: float
    reason: str
    method: str  # heuristic | llm | manual


class CorrectionEngine:
    def __init__(
        self,
        db: DatabaseManager,
        glossary: GlossaryManager,
        provider: TranslationProvider | None = None,
        llm_min_confidence: float = 0.4,
        llm_batch_size: int = 20,
    ) -> None:
        self._db = db
        self._glossary = glossary
        self._provider = provider
        self._llm_min_confidence = llm_min_confidence
        self._llm_batch_size = llm_batch_size

    # ------------------------------------------------------------- heuristics
    def scan_heuristics(
        self, doc: SubtitleDocument, project_id: int, store: bool = True
    ) -> list[CorrectionProposal]:
        terms = [entry.english_term for entry in self._glossary.all_terms()]
        proposals: list[CorrectionProposal] = []
        for entry in doc.entries:
            text = entry.original_text
            if not text.strip():
                continue
            proposals.extend(self._stutter_proposals(entry, text))
            proposals.extend(self._glossary_distance_proposals(entry, text, terms))
            proposals.extend(self._echo_proposals(doc, entry))
        if store:
            for proposal in proposals:
                self._db.correction_add(
                    CorrectionRecord(
                        project_id=project_id,
                        position=proposal.position,
                        original_text=proposal.original_text,
                        proposed_text=proposal.proposed_text,
                        confidence=proposal.confidence,
                        reason=proposal.reason,
                        method=proposal.method,
                    )
                )
        log.info("Heuristic scan found %d suspicious spots", len(proposals))
        return proposals

    def _stutter_proposals(self, entry, text: str) -> list[CorrectionProposal]:
        proposals = []
        selected = []
        for match in _STUTTER_RE.finditer(text):
            word = match.group(1)
            repeats = len(re.findall(re.escape(word), match.group(0), flags=re.IGNORECASE))
            if repeats >= 3 or word.lower() in _COMMON_DOUBLES:
                selected.append(match)
        if not selected:
            return proposals
        corrected = text
        for match in reversed(selected):  # rebuild right-to-left to keep offsets
            corrected = corrected[: match.start()] + match.group(1) + corrected[match.end():]
        repeats = len(selected)
        proposals.append(
            CorrectionProposal(
                position=entry.position,
                original_text=text,
                proposed_text=corrected,
                confidence=0.9 if repeats >= 2 else 0.75,
                reason=f"Repeated word stutter ({repeats} spot(s)); likely ASR artifact",
                method="heuristic",
            )
        )
        return proposals

    def _glossary_distance_proposals(
        self, entry, text: str, terms: list[str]
    ) -> list[CorrectionProposal]:
        """ASR errors usually hit a single word, so each word is compared both to
        full terms and to the individual words of multi-word terms."""
        if not terms:
            return []
        candidates: list[tuple[str, str]] = []  # (candidate word, source term)
        for term in terms:
            candidates.append((term.lower(), term))
            for part in term.split():
                candidates.append((part.lower(), term))
        proposals = []
        for word in set(english_words(text)):
            lowered = word.lower()
            if len(lowered) < 5:
                continue
            best_word, best_term, best_distance = None, None, None
            for candidate, source_term in candidates:
                if abs(len(candidate) - len(lowered)) > 3:
                    continue
                distance = edit_distance(lowered, candidate)
                limit = 1 if len(candidate) <= 6 else (2 if len(candidate) <= 10 else 3)
                if 0 < distance <= limit and (best_distance is None or distance < best_distance):
                    best_word, best_term, best_distance = candidate, source_term, distance
            if best_word is None:
                continue
            corrected = re.sub(
                rf"(?<!\w){re.escape(word)}(?!\w)", best_word, text, flags=re.IGNORECASE
            )
            origin = f" (from {best_term!r})" if best_term != best_word else ""
            proposals.append(
                CorrectionProposal(
                    position=entry.position,
                    original_text=text,
                    proposed_text=corrected,
                    confidence=_DISTANCE_CONFIDENCE[best_distance],
                    reason=(
                        f"Word {word!r} is close to glossary term {best_word!r}{origin} "
                        f"(edit distance {best_distance})"
                    ),
                    method="heuristic",
                )
            )
        return proposals

    def _echo_proposals(self, doc: SubtitleDocument, entry) -> list[CorrectionProposal]:
        if entry.position == 0:
            return []
        previous = doc.entries[entry.position - 1]
        if (
            previous.original_text.strip()
            and normalize_for_match(previous.original_text)
            == normalize_for_match(entry.original_text)
        ):
            return [
                CorrectionProposal(
                    position=entry.position,
                    original_text=entry.original_text,
                    proposed_text=entry.original_text,  # flag only; no silent change
                    confidence=0.4,
                    reason="Identical to the previous caption (possible ASR echo) - review",
                    method="heuristic",
                )
            ]
        return []

    # ------------------------------------------------------------ LLM review
    def review_with_llm(
        self,
        doc: SubtitleDocument,
        project_id: int,
        positions: list[int] | None = None,
    ) -> list[CorrectionProposal]:
        """Ask the provider to review captions in context. Stores proposals for
        every suspected line; returns the newly created proposals."""
        if self._provider is None:
            raise ConfigurationError(
                "LLM review needs a configured provider; set up the API first."
            )
        if positions is None:
            positions = [e.position for e in doc.entries if e.original_text.strip()]
        terms = [entry.english_term for entry in self._glossary.all_terms()]
        proposals: list[CorrectionProposal] = []

        for start in range(0, len(positions), self._llm_batch_size):
            chunk = positions[start: start + self._llm_batch_size]
            payload = {
                "previous": [
                    doc.entries[p].original_text
                    for p in range(max(0, chunk[0] - 2), chunk[0])
                ],
                "captions": [
                    {"i": p, "text": doc.entries[p].original_text} for p in chunk
                ],
                "glossary": terms,
            }
            request = ProviderRequest(
                model="",
                messages=[
                    ChatMessage(role="system", content=REVIEW_SYSTEM_PROMPT),
                    ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
                ],
                json_mode=True,
                payload=payload,
                metadata={"purpose": "asr_review", "positions": chunk},
            )
            response = self._provider.translate(request)
            proposals.extend(self._parse_review(doc, response.content, chunk))

        for proposal in proposals:
            self._db.correction_add(
                CorrectionRecord(
                    project_id=project_id,
                    position=proposal.position,
                    original_text=proposal.original_text,
                    proposed_text=proposal.proposed_text,
                    confidence=proposal.confidence,
                    reason=proposal.reason,
                    method="llm",
                )
            )
        return proposals

    def _parse_review(
        self, doc: SubtitleDocument, content: str, chunk: list[int]
    ) -> list[CorrectionProposal]:
        text = content.strip()
        if "```" in text:
            import re as _re

            fence = _re.search(r"```(?:json)?\s*(.*?)\s*```", text, _re.DOTALL)
            if fence:
                text = fence.group(1)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            log.warning("LLM review returned invalid JSON: %s", exc)
            return []
        reviews = data.get("reviews") if isinstance(data, dict) else None
        if not isinstance(reviews, list):
            log.warning("LLM review JSON has unexpected shape")
            return []

        proposals = []
        chunk_set = set(chunk)
        for review in reviews:
            if not isinstance(review, dict):
                continue
            try:
                index = int(review.get("i"))
            except (TypeError, ValueError):
                continue
            if index not in chunk_set:
                continue
            if not review.get("suspect"):
                continue
            try:
                confidence = float(review.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < self._llm_min_confidence:
                continue
            original = doc.entries[index].original_text
            corrected = str(review.get("correction") or "").strip()
            if not corrected or corrected == original:
                continue
            proposals.append(
                CorrectionProposal(
                    position=index,
                    original_text=original,
                    proposed_text=corrected,
                    confidence=confidence,
                    reason=str(review.get("reason") or ""),
                    method="llm",
                )
            )
        return proposals

    # ------------------------------------------------------- approve / reject
    def approve(self, doc: SubtitleDocument, correction_id: int) -> CorrectionRecord:
        record = self._db.correction_get(correction_id)
        self._db.correction_set_status(correction_id, "approved")
        entry = doc.get(record.position)
        entry.working_text = record.proposed_text
        entry.correction_id = correction_id
        return record

    def reject(self, doc: SubtitleDocument, correction_id: int) -> CorrectionRecord:
        record = self._db.correction_get(correction_id)
        self._db.correction_set_status(correction_id, "rejected")
        entry = doc.get(record.position)
        if entry.correction_id == correction_id:
            # revert working text to the pristine original
            entry.working_text = entry.original_text
            entry.correction_id = None
        return record
