"""Runtime configuration.

Everything is env-overridable. The corpus root is additionally persisted to
``data/settings.json`` so it survives restarts once set from the web UI.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser().resolve()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: _env_path("DD_DATA_DIR", "./data"))

    # Models. Analyst does the reasoning; carder does the bulk indexing pass;
    # verifier re-reads cited spans. See README for the cost rationale.
    analyst_model: str = os.environ.get("DD_ANALYST_MODEL", "claude-opus-5")
    carder_model: str = os.environ.get("DD_CARDER_MODEL", "claude-haiku-4-5")
    verifier_model: str = os.environ.get("DD_VERIFIER_MODEL", "claude-haiku-4-5")
    analyst_effort: str = os.environ.get("DD_ANALYST_EFFORT", "high")

    # Concurrency
    extract_workers: int = _env_int("DD_EXTRACT_WORKERS", max(2, (os.cpu_count() or 4) - 1))
    card_concurrency: int = _env_int("DD_CARD_CONCURRENCY", 8)
    verify_concurrency: int = _env_int("DD_VERIFY_CONCURRENCY", 8)

    # The manifest is the always-in-context map of the corpus. Beyond this many
    # characters we fall back to a rollup + the list_documents tool.
    manifest_char_budget: int = _env_int("DD_MANIFEST_CHAR_BUDGET", 1_000_000)

    # Per-document text handed to the carder for summarisation.
    card_excerpt_chars: int = _env_int("DD_CARD_EXCERPT_CHARS", 24_000)

    ocr_enabled: bool = _env_bool("DD_OCR", False)
    enable_python_tool: bool = _env_bool("DD_ENABLE_PYTHON", True)
    python_timeout_s: int = _env_int("DD_PYTHON_TIMEOUT", 90)

    max_file_mb: int = _env_int("DD_MAX_FILE_MB", 256)

    def __post_init__(self) -> None:
        for d in (self.data_dir, self.derived_dir, self.artifacts_dir):
            d.mkdir(parents=True, exist_ok=True)

    # --- derived paths ---------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.sqlite3"

    @property
    def derived_dir(self) -> Path:
        return self.data_dir / "derived"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "settings.json"

    def text_path(self, doc_id: str) -> Path:
        return self.derived_dir / f"{doc_id}.md"

    # --- mutable, UI-settable state --------------------------------------
    def _state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    def _write_state(self, patch: dict) -> None:
        state = self._state()
        state.update(patch)
        self.state_path.write_text(json.dumps(state, indent=2))

    @property
    def corpus_root(self) -> Path | None:
        raw = os.environ.get("DD_CORPUS_ROOT") or self._state().get("corpus_root")
        return Path(raw).expanduser().resolve() if raw else None

    def set_corpus_root(self, path: str) -> Path:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"not a directory: {resolved}")
        self._write_state({"corpus_root": str(resolved)})
        return resolved

    def has_api_key(self) -> bool:
        """True when the SDK will find credentials: env var, or an `ant auth login` profile."""
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return True
        cfg = Path(os.environ.get("ANTHROPIC_CONFIG_DIR", Path.home() / ".config" / "anthropic"))
        creds = cfg / "credentials"
        return creds.is_dir() and any(creds.glob("*.json"))


settings = Settings()

SUPPORTED_EXTS = {
    ".pdf": "pdf",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".xls": "xlsx",
    ".pptx": "pptx",
    ".docx": "docx",
    ".csv": "text",
    ".tsv": "text",
    ".txt": "text",
    ".md": "text",
    ".json": "text",
}

WORKSTREAMS = [
    "financial",
    "legal",
    "commercial",
    "tax",
    "hr",
    "it",
    "operations",
    "esg",
    "insurance",
    "other",
]
