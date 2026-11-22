"""Text helpers shared by the parser, memory, corrections and engine."""
from __future__ import annotations

import hashlib
import re
import unicodedata

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\u2019\-]*")

AMBIGUOUS_RE = re.compile(
    r"^\s*(it|this|that|he|she|they|we|you|really|seriously|oh|wow|so|well|"
    r"yeah|ok|okay|sure|right)\s*[.?!]?\s*$",
    re.IGNORECASE,
)


def normalize_for_match(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace.

    Used as the translation-memory matching key, so punctuation-only or
    case-only differences still match.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u2019", "'").replace("`", "'")
    text = _PUNCT_RE.sub(" ", text.lower())
    return _WS_RE.sub(" ", text).strip()


def stable_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance with O(min(len)) memory."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) > len(b):
        a, b = b, a
    previous = list(range(len(a) + 1))
    for j, char_b in enumerate(b, 1):
        current = [j]
        for i, char_a in enumerate(a, 1):
            current.append(
                min(
                    previous[i] + 1,        # deletion
                    current[i - 1] + 1,     # insertion
                    previous[i - 1] + (char_a != char_b),  # substitution
                )
            )
        previous = current
    return previous[-1]


def english_words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def looks_ambiguous(text: str) -> bool:
    """Short or pronoun-only lines depend heavily on context; the translation
    memory must be strict about reusing them."""
    stripped = (text or "").strip()
    if not stripped:
        return True
    if len(stripped) <= 25:
        return True
    return bool(AMBIGUOUS_RE.match(stripped))
