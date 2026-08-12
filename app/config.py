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


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
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
    # "auto" (the default) marks the session cookie Secure when the request
    # itself arrived over HTTPS, which behind a trusted proxy means when
    # X-Forwarded-Proto says https. Getting this wrong either way is a silent
    # failure — a Secure cookie on http:// is dropped by the browser, so login
    # appears to succeed and every later request is 401 — so deriving it beats
    # asking every deployment to remember. "1"/"0" force it.
    cookie_secure: str = os.environ.get("DD_COOKIE_SECURE", "auto").strip().lower()

    # --- archive upload / extraction limits ---
    max_upload_mb: int = _env_int("DD_MAX_UPLOAD_MB", 4096)
    max_extract_gb: int = _env_int("DD_MAX_EXTRACT_GB", 20)
    max_archive_members: int = _env_int("DD_MAX_ARCHIVE_MEMBERS", 100_000)
    max_compression_ratio: int = _env_int("DD_MAX_COMPRESSION_RATIO", 200)

    # --- remote library sync (SharePoint) --------------------------------
    rclone_bin: str = os.environ.get("DD_RCLONE_BIN", "rclone")
    # Refuse to start a sync whose remote side is larger than this. The mirror is
    # a full second copy of the library on the data volume.
    max_sync_gb: int = _env_int("DD_MAX_SYNC_GB", 50)
    # A sync mirrors deletions, so a connection pointed at the wrong library
    # could empty the mirror. rclone aborts past this many deletions in one run.
    max_sync_delete: int = _env_int("DD_MAX_SYNC_DELETE", 500)
    sync_timeout_s: int = _env_int("DD_SYNC_TIMEOUT", 6 * 3600)
    # Run history per connection. "What changed last night" is asked about
    # recent syncs, so this is a window rather than an archive.
    sync_runs_keep: int = _env_int("DD_SYNC_RUNS_KEEP", 50)

    # --- activity log ----------------------------------------------------
    # How many log lines to keep. A sweep over a large data room writes one line
    # per problem file, and the point of keeping them is to still have the
    # failures after a restart, so this is generous. 0 disables trimming.
    log_retention: int = _env_int("DD_LOG_RETENTION", 20_000)

    # --- weekly spending limits ------------------------------------------
    # Two budgets, because the two ways of spending money here are nothing alike:
    # asking questions is small and constant, indexing a library is one large
    # deliberate act. A single pooled figure would either be too small to sweep
    # with or too large to be a limit on questions.
    #
    # These are the instance defaults an account inherits when it has no explicit
    # setting of its own. -1 is unlimited, and is the default so that adding this
    # feature changes nothing until someone sets a number. 0 means no spending.
    weekly_budget_ask_usd: float = _env_float("DD_WEEKLY_BUDGET_ASK_USD", -1.0)
    weekly_budget_index_usd: float = _env_float("DD_WEEKLY_BUDGET_INDEX_USD", -1.0)
    # An answer that stops halfway has spent money and produced nothing, so a user
    # may overrun the ask budget by this much to finish one — once per week.
    budget_grace_pct: float = _env_float("DD_BUDGET_GRACE_PCT", 10.0)

    def __post_init__(self) -> None:
        for d in (
            self.data_dir,
            self.derived_dir,
            self.artifacts_dir,
            self.uploads_dir,
            self.archives_dir,
            self.extract_root,
            self.sync_root,
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
    def sync_root(self) -> Path:
        """Where remote libraries are mirrored, one directory per connection.

        Each mirror is an ordinary corpus root, so everything downstream of
        ingest treats a synced library exactly like a local folder.
        """
        return self.data_dir / "sync"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def browse_roots(self) -> list[Path]:
        """Directories the folder picker may descend into.

        Deliberately narrow: without this the picker is a filesystem browser for
        anyone who can reach the app. Override with DD_BROWSE_ROOTS (os.pathsep
        separated) to say which *external* directories are in bounds.

        The two directories the app fills itself — expanded archives and synced
        libraries — are always in bounds, whatever DD_BROWSE_ROOTS says. They
        contain nothing the app did not put there, and leaving them out of an
        explicit list is a misconfiguration whose only symptom is that uploads
        and syncs cannot be indexed.
        """
        raw = os.environ.get("DD_BROWSE_ROOTS")
        if raw:
            roots = [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]
            roots += [self.extract_root, self.sync_root]
        else:
            roots = [self.extract_root, self.sync_root]
            for candidate in (Path("/corpus"), Path("/inbox"), Path.home() / "corpus", Path.cwd()):
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
