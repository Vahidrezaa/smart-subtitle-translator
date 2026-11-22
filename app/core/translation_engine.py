"""Translation engine: prompt building, provider call, response validation.

The engine is deliberately transport-agnostic: it calls the provider once per
batch and raises typed provider errors — retries and backoff live in
``batch_processor`` so the policy is testable without a real API.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from app.core.context_manager import ContextManager
from app.core.events import ProgressEvent, WarningEvent
from app.core.glossary_manager import GlossaryManager
from app.core.languages import get_language
from app.core.subtitle_model import EntryStatus, SubtitleDocument
from app.providers.base_provider import (
    ChatMessage,
    ProviderBadResponseError,
    ProviderRequest,
    ProviderResponse,
    TranslationProvider,
)

log = logging.getLogger(__name__)

_SYSTEM_PROMPT_TEMPLATE = """You are a professional subtitle translator translating a video's English subtitles into {language_name}.

RULES - follow all of them:
1. Translate meaning, not words. Natural, spoken {language_name} beats literal, formal or literary phrasing.
2. Preserve the speaker's tone: humor, sarcasm, excitement, frustration, informality.
3. Idioms and slang: translate what they MEAN in this context, never word by word.
4. Never invent information that is not in the source, and never omit meaningful content.
5. Keep each line short and subtitle-friendly; it must be readable within the original timing.
6. Technical terminology must stay consistent across the whole video. When a "glossary" entry applies, you MUST use its preferred translation or one of its listed alternatives. The glossary overrides your generic preference.
7. When {language_name}-speaking developers naturally keep an English technical term (API names, library names, CLI commands, protocol names), keep it in English instead of forcing a translation.
8. Do not censor or soften ordinary language, and never change the technical meaning to make it sound smoother.
9. Use "previous" lines (which may already include their approved translations) and "upcoming" lines only to disambiguate meaning and keep terminology consistent.
10. Translate each numbered line separately. Never merge, split, reorder or renumber lines.
11. Output ONLY the JSON object described below. No commentary, no markdown fences.

OUTPUT FORMAT (exactly this shape):
{{"translations": [{{"i": <line number>, "fa": "<translation>"}}]}}
"i" must be one of the line numbers given in "lines". Every line in "lines" must appear exactly once.
The translation key is "fa" (target language: {language_name})."""

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_FA_KEYS = ("fa", "fa_text", "persian", "translation")
_INDEX_KEYS = ("i", "index", "id", "position")


class IncompleteTranslationError(ProviderBadResponseError):
    """The model's answer did not cover every requested line."""


def system_prompt(target_language: str = "fa") -> str:
    return _SYSTEM_PROMPT_TEMPLATE.replace(
        "{language_name}", get_language(target_language).prompt_name
    )


@dataclass
class LineResult:
    position: int
    persian: str
    status: EntryStatus
    warnings: list[str] = field(default_factory=list)


@dataclass
class BatchResult:
    results: dict[int, LineResult] = field(default_factory=dict)  # by position
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    glossary_version: str = ""


class PromptBuilder:
    def __init__(self, target_language: str = "fa") -> None:
        self.target_language = target_language

    def system_message(self) -> ChatMessage:
        return ChatMessage(role="system", content=system_prompt(self.target_language))

    def user_message(self, context: dict) -> ChatMessage:
        return ChatMessage(role="user", content=json.dumps(context, ensure_ascii=False))


def parse_translations(content: str, requested_positions: set[int]) -> dict[int, str]:
    """Parse the model answer into {position: persian}. Tolerates markdown fences,
    leading chatter, "translations" wrapper or a bare list; raises
    IncompleteTranslationError when requested lines are missing or empty."""
    text = content.strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    else:
        starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
        if starts and min(starts) > 0:
            text = text[min(starts):]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderBadResponseError(f"Translation response is not valid JSON: {exc}") from exc

    items = None
    if isinstance(data, dict):
        items = data.get("translations")
    elif isinstance(data, list):
        items = data
    if not isinstance(items, list):
        raise ProviderBadResponseError(
            'Expected {"translations": [...]} in the response'
        )

    parsed: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        index = next((item[k] for k in _INDEX_KEYS if k in item), None)
        text_value = next((item[k] for k in _FA_KEYS if item.get(k)), None)
        if index is None or text_value is None:
            continue
        try:
            index = int(index)
        except (TypeError, ValueError):
            continue
        if str(text_value).strip():
            parsed[index] = str(text_value).strip()

    missing = requested_positions - set(parsed)
    if missing:
        raise IncompleteTranslationError(
            f"The response is missing translations for lines: {sorted(missing)}"
        )
    return parsed


class TranslationEngine:
    """Translates one batch of positions using context + glossary."""

    MAX_FA_VS_EN_RATIO = 6.0  # crude hallucination guard (warning only)

    def __init__(
        self,
        provider: TranslationProvider,
        glossary: GlossaryManager,
        context_manager: ContextManager,
        target_language: str = "fa",
        listener: ProgressEvent | None = None,
    ) -> None:
        self.provider = provider
        self.glossary = glossary
        self.context_manager = context_manager
        self.target_language = target_language
        self.listener = listener
        self.prompt_builder = PromptBuilder(target_language)

    def _emit(self, event: ProgressEvent) -> None:
        if self.listener:
            try:
                self.listener(event)
            except Exception:
                log.exception("Progress listener raised")

    def translate_positions(
        self,
        doc: SubtitleDocument,
        positions: list[int],
        model: str = "",
        temperature: float | None = None,
        json_mode: bool = True,
        timeout: float = 60.0,
    ) -> BatchResult:
        positions = sorted(positions)
        if not positions:
            return BatchResult()

        glossary_payload = self._glossary_payload(doc, positions)
        context = self.context_manager.build(doc, positions, glossary_payload)
        context_dict = context.to_prompt_dict()

        request = ProviderRequest(
            model=model,
            messages=[
                self.prompt_builder.system_message(),
                self.prompt_builder.user_message(context_dict),
            ],
            temperature=temperature,
            json_mode=json_mode,
            timeout=timeout,
            payload=context_dict,
            metadata={
                "positions": positions,
                "glossary_version": self.glossary.version(),
                "target_language": self.target_language,
            },
        )
        response = self.provider.translate(request)
        translations = parse_translations(response.content, set(positions))

        results: dict[int, LineResult] = {}
        for position in positions:
            persian = translations[position]
            warnings = self._check_line(doc, position, persian, glossary_payload)
            if warnings:
                self._emit(WarningEvent(
                    message=f"Line {position}: " + "; ".join(warnings),
                    position=position,
                ))
            results[position] = LineResult(
                position=position, persian=persian,
                status=EntryStatus.MACHINE, warnings=warnings,
            )

        return BatchResult(
            results=results,
            model=response.model,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            glossary_version=self.glossary.version(),
        )

    # ------------------------------------------------------------- internals
    def _glossary_payload(self, doc: SubtitleDocument, positions: list[int]) -> list[dict]:
        window_start = max(0, positions[0] - self.context_manager.prev_lines)
        texts = [doc.entries[p].working_text for p in range(window_start, positions[-1] + 1)]
        hits: dict[int, object] = {}
        for text in texts:
            for hit in self.glossary.find_terms(text):
                hits[hit.entry.id] = hit
        return [
            {
                "en": hit.entry.english_term,
                "fa": hit.entry.persian_primary,
                "alt": list(hit.entry.persian_alternatives),
                "category": hit.entry.category,
            }
            for hit in hits.values()
        ]

    def _check_line(
        self, doc: SubtitleDocument, position: int, persian: str, glossary_payload: list[dict]
    ) -> list[str]:
        warnings = []
        english = doc.entries[position].working_text
        ratio = len(persian) / max(1, len(english))
        if len(english) >= 10 and ratio > self.MAX_FA_VS_EN_RATIO:
            warnings.append(
                f"translation is {ratio:.1f}x longer than the source - possible hallucination"
            )
        lower_english = english.lower()
        for item in glossary_payload:
            term = item["en"].lower()
            if not re.search(rf"(?<!\w){re.escape(term)}(?!\w)", lower_english):
                continue
            variants = [item["fa"]] + list(item.get("alt") or [])
            if not any(variant and variant in persian for variant in variants):
                warnings.append(
                    f"glossary term {item['en']!r} was not translated with a preferred equivalent"
                )
        return warnings
