"""Target-language registry.

v1 ships Persian only (the prompts and rules are tuned for it). Adding a
language later means adding one entry here — GUI and prompts are generated
from this registry.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageInfo:
    code: str
    english_name: str
    native_name: str
    rtl: bool
    prompt_name: str


LANGUAGES: dict[str, LanguageInfo] = {
    "fa": LanguageInfo(
        code="fa",
        english_name="Persian",
        native_name="فارسی",
        rtl=True,
        prompt_name="Persian (Farsi)",
    ),
}

DEFAULT_LANGUAGE = "fa"


def get_language(code: str) -> LanguageInfo:
    return LANGUAGES.get(code, LANGUAGES[DEFAULT_LANGUAGE])
