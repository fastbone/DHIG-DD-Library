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

    # --- accounts ---
    session_ttl_hours: int = _env_int("DD_SESSION_TTL_HOURS", 12)
    login_max_attempts: int = _env_int("DD_LOGIN_MAX_ATTEMPTS", 8)
    login_lockout_s: int = _env_int("DD_LOGIN_LOCKOUT_SECONDS", 300)
    cookie_secure: bool = _env_bool("DD_COOKIE_SECURE", False)

    # --- archive upload / extraction limits ---
    max_upload_mb: int = _env_int("DD_MAX_UPLOAD_MB", 4096)
    max_extract_gb: int = _env_int("DD_MAX_EXTRACT_GB", 20)
    max_archive_members: int = _env_int("DD_MAX_ARCHIVE_MEMBERS", 100_000)
    max_compression_ratio: int = _env_int("DD_MAX_COMPRESSION_RATIO", 200)

    def __post_init__(self) -> None:
        for d in (
            self.data_dir,
            self.derived_dir,
            self.artifacts_dir,
            self.uploads_dir,
            self.archives_dir,
            self.extract_root,
        ):
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
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def archives_dir(self) -> Path:
        """Uploaded archives, kept so an extraction can be repeated or audited."""
        return self.uploads_dir / "archives"

    @property
    def extract_root(self) -> Path:
        """Where uploaded archives are expanded. A corpus root may live here."""
        return self.uploads_dir / "extracted"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def browse_roots(self) -> list[Path]:
        """Directories the folder picker may descend into.

        Deliberately narrow: without this the picker is a filesystem browser for
        anyone who can reach the app. Override with DD_BROWSE_ROOTS (os.pathsep
        separated).
        """
        raw = os.environ.get("DD_BROWSE_ROOTS")
        if raw:
            roots = [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]
        else:
            roots = [self.extract_root]
            for candidate in (Path("/corpus"), Path.home() / "corpus", Path.cwd()):
                if candidate.is_dir():
                    roots.append(candidate)
            if self.corpus_root:
                roots.append(self.corpus_root)
        seen: list[Path] = []
        for r in roots:
            try:
                resolved = r.resolve()
            except OSError:
                continue
            if resolved.is_dir() and resolved not in seen:
                seen.append(resolved)
        return seen

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
        # Remember every root we have pointed at, so the storage panel can list
        # a folder that was ingested earlier and is no longer active.
        history = [h for h in self.root_history if h != str(resolved)]
        history.insert(0, str(resolved))
        self._write_state({"corpus_root": str(resolved), "root_history": history[:20]})
        return resolved

    def clear_corpus_root(self) -> None:
        self._write_state({"corpus_root": None})

    @property
    def root_history(self) -> list[str]:
        value = self._state().get("root_history") or []
        return [v for v in value if isinstance(v, str)]

    def forget_root(self, path: str) -> None:
        target = str(Path(path).expanduser().resolve())
        self._write_state({"root_history": [h for h in self.root_history if h != target]})

    def has_api_key(self) -> bool:
        """True when the SDK will find credentials: env var, or an `ant auth login` profile."""
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return True
        cfg = Path(os.environ.get("ANTHROPIC_CONFIG_DIR", Path.home() / ".config" / "anthropic"))
        creds = cfg / "credentials"
        try:
            return creds.is_dir() and any(creds.glob("*.json"))
        except OSError:
            # An unreadable home directory is a deployment quirk, not a reason to
            # refuse to start: a container can be handed a home it cannot stat.
            # Keys stored in the app still work.
            return False


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
