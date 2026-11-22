"""Typed application settings (no secrets here — API keys live in credential stores)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ApiConfig:
    provider: str = "openai"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    temperature: float = 0.2
    timeout_seconds: float = 60.0
    json_mode: bool = True
    # auto | env | keyring | file
    credential_backend: str = "auto"


@dataclass
class TranslationConfig:
    target_language: str = "fa"
    batch_max_lines: int = 15
    batch_max_chars: int = 3500
    context_prev_lines: int = 3
    context_future_lines: int = 2
    max_retries: int = 4
    retry_base_delay: float = 2.0
    # Reserved for future character-specific style profiles.
    style_note: str = ""


@dataclass
class MemoryConfig:
    reuse_enabled: bool = True
    min_affinity_approved: float = 0.5
    min_affinity_corrected: float = 0.8
    min_affinity_machine: float = 0.9


@dataclass
class CorrectionConfig:
    heuristic_scan: bool = True
    llm_review: bool = False  # off by default: costs API calls
    llm_min_confidence_suggest: float = 0.4


@dataclass
class ExportConfig:
    bom: bool = True  # UTF-8 BOM helps players detect UTF-8 reliably (PotPlayer included)
    use_crlf: bool = True


@dataclass
class Settings:
    api: ApiConfig = field(default_factory=ApiConfig)
    translation: TranslationConfig = field(default_factory=TranslationConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    corrections: CorrectionConfig = field(default_factory=CorrectionConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
