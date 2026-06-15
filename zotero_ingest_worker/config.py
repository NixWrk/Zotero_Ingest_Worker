from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .worker_roles import ROLE_ALL, normalize_worker_role


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path | None = None) -> None:
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return float(value)


def env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


def env_first_path(names: tuple[str, ...], default: Path) -> Path:
    for name in names:
        value = os.environ.get(name)
        if value:
            return Path(value)
    return default


def env_path_list(name: str) -> tuple[Path, ...]:
    value = os.environ.get(name, "").strip()
    if not value:
        return ()
    return tuple(Path(part.strip()) for part in value.split(";") if part.strip())


def env_string_prefix_map(name: str) -> tuple[tuple[str, str], ...]:
    value = os.environ.get(name, "").strip()
    if not value:
        return ()
    result: list[tuple[str, str]] = []
    for raw_part in value.split(";"):
        part = raw_part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"Invalid {name} entry: {part!r}. Expected source_prefix=target_prefix.")
        source_prefix, target_prefix = part.split("=", 1)
        source_prefix = source_prefix.strip().rstrip("\\/")
        target_prefix = target_prefix.strip().rstrip("\\/")
        if not source_prefix or not target_prefix:
            raise ValueError(f"Invalid {name} entry: {part!r}. Expected source_prefix=target_prefix.")
        result.append((source_prefix, target_prefix))
    return tuple(result)


def unique_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        normalized = str(path.expanduser()).rstrip("\\/").lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(path)
    return tuple(result)


def default_discovery_roots() -> tuple[Path, ...]:
    roots = [Path.home() / "Zotero"]
    if os.name == "nt":
        roots.append(Path(r"C:\PC\Zotero"))
    else:
        roots.extend([Path("/zotero_roots"), Path("/zotero_root"), Path("/zotero")])
    return unique_paths(tuple(roots))


def is_zotero_data_dir(path: Path) -> bool:
    return (path / "zotero.sqlite").is_file() and (path / "storage").is_dir()


def discover_zotero_data_dirs(
    roots: tuple[Path, ...],
    *,
    max_depth: int,
) -> tuple[Path, ...]:
    found: list[Path] = []
    for root in roots:
        expanded = root.expanduser()
        if not expanded.exists():
            continue
        stack: list[tuple[Path, int]] = [(expanded, 0)]
        while stack:
            current, depth = stack.pop()
            if is_zotero_data_dir(current):
                found.append(current)
                continue
            if depth >= max_depth:
                continue
            try:
                children = sorted(
                    (child for child in current.iterdir() if child.is_dir()),
                    key=lambda child: child.name.lower(),
                    reverse=True,
                )
            except OSError:
                continue
            for child in children:
                stack.append((child, depth + 1))
    return unique_paths(tuple(found))


@dataclass(frozen=True)
class WorkerConfig:
    zotero_data_dir: Path
    zotero_data_dirs: tuple[Path, ...]
    zotero_discovery_roots: tuple[Path, ...]
    zotero_discovery_max_depth: int
    zotero_path_prefix_map: tuple[tuple[str, str], ...]
    zotero_storage_dir: Path | None

    worker_host: str
    worker_port: int
    worker_token: str
    worker_role: str

    ingest_data_root: Path
    html_data_root: Path
    zotero_parent_preflight_enabled: bool

    metadata_crossref_email: str
    metadata_unpaywall_email: str
    metadata_openalex_api_key: str
    metadata_semantic_scholar_api_key: str
    metadata_core_api_key: str
    metadata_extended_providers_enabled: bool
    metadata_request_timeout_seconds: int
    metadata_user_agent: str
    metadata_policy: str
    metadata_title_min_score: float
    metadata_job_lease_seconds: int
    zotero_translation_server_url: str
    zotero_translation_server_timeout_seconds: int

    arxiv_html_root: Path
    arxiv_html_attach: bool
    arxiv_html_fetch_timeout_seconds: int
    arxiv_html_min_text_chars: int
    arxiv_search_min_score: float

    scihub_enabled: bool
    scihub_base_url: str
    scihub_mirrors: tuple[str, ...]
    scihub_user_agent: str
    scihub_request_timeout_seconds: int

    pdf_text_min_chars: int
    pdf_text_check_pages: int

    scan_limit: int
    scan_max_items: int
    retry_failed: bool
    request_timeout_seconds: int
    zotero_relay_url: str
    zotero_relay_token: str
    zotero_relay_replace_strategy: str

    @property
    def zotero_sqlite_path(self) -> Path:
        return self.zotero_data_dir / "zotero.sqlite"

    @property
    def resolved_storage_dir(self) -> Path:
        return self.zotero_storage_dir or (self.zotero_data_dir / "storage")

    @property
    def ocr_data_root(self) -> Path:
        # Compatibility alias for older full-text download paths.
        return self.ingest_data_root

    @property
    def downloads_root(self) -> Path:
        return self.ingest_data_root / "downloads"

    @property
    def state_db_path(self) -> Path:
        return self.ingest_data_root / "state.sqlite"

    @property
    def zotero_relay_enabled(self) -> bool:
        return bool(self.zotero_relay_url and self.zotero_relay_replace_strategy)

    def translate_zotero_input_path(self, raw_path: str | Path) -> Path:
        raw = str(raw_path).strip().strip('"').strip("'")
        normalized_raw = _normalize_prefix_path(raw)
        for source_prefix, target_prefix in self.zotero_path_prefix_map:
            normalized_source = _normalize_prefix_path(source_prefix)
            if normalized_raw == normalized_source:
                return Path(target_prefix)
            if normalized_raw.startswith(f"{normalized_source}/"):
                relative = normalized_raw[len(normalized_source) + 1 :]
                return Path(target_prefix) / Path(*relative.split("/"))
        return Path(raw)

    def validate_for_scan(self) -> None:
        for data_dir in self.zotero_data_dirs:
            if not data_dir.exists():
                raise ValueError(f"Zotero data directory does not exist: {data_dir}")
            if not (data_dir / "zotero.sqlite").exists():
                raise ValueError(f"zotero.sqlite was not found: {data_dir / 'zotero.sqlite'}")
            if not (data_dir / "storage").exists():
                raise ValueError(f"Zotero storage directory does not exist: {data_dir / 'storage'}")


DEFAULT_SCIHUB_MIRRORS = (
    "https://sci-hub.ru/",
    "https://sci-hub.se/",
    "https://sci-hub.st/",
)


def parse_scihub_mirrors(raw: str, *, base_url: str) -> tuple[str, ...]:
    entries = [part.strip() for part in str(raw or "").split(";") if part.strip()]
    if not entries:
        entries = [base_url, *DEFAULT_SCIHUB_MIRRORS]
    else:
        entries = [base_url, *entries]
    seen: set[str] = set()
    mirrors: list[str] = []
    for entry in entries:
        normalized = entry if entry.endswith("/") else f"{entry}/"
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        mirrors.append(normalized)
    return tuple(mirrors)


def from_env(*, load_file: bool = True) -> WorkerConfig:
    if load_file:
        load_dotenv()

    primary_data_dir = env_path("ZOTERO_DATA_DIR", Path.home() / "Zotero")
    explicit_data_dirs = env_path_list("ZOTERO_DATA_DIRS")
    discovery_roots = env_path_list("ZOTERO_DISCOVERY_ROOTS") or default_discovery_roots()
    discovery_max_depth = env_int("ZOTERO_DISCOVERY_MAX_DEPTH", 4)
    auto_discover = env_bool("ZOTERO_AUTO_DISCOVER", True)
    discovered_data_dirs = (
        discover_zotero_data_dirs(discovery_roots, max_depth=discovery_max_depth)
        if auto_discover
        else ()
    )
    if explicit_data_dirs:
        data_dirs = unique_paths(explicit_data_dirs)
    else:
        data_dirs = discovered_data_dirs
    if not data_dirs and is_zotero_data_dir(primary_data_dir):
        data_dirs = (primary_data_dir,)
    if not data_dirs:
        data_dirs = (primary_data_dir,)

    scihub_base_url = os.environ.get("SCIHUB_BASE_URL", "https://sci-hub.ru/").strip() or "https://sci-hub.ru/"
    scihub_mirrors = parse_scihub_mirrors(os.environ.get("SCIHUB_MIRRORS", ""), base_url=scihub_base_url)
    ingest_data_root = env_first_path(
        ("INGEST_DATA_ROOT", "ZOTERO_INGEST_DATA_ROOT", "OCR_DATA_ROOT"),
        PROJECT_ROOT / "data" / "ingest",
    )

    return WorkerConfig(
        zotero_data_dir=data_dirs[0],
        zotero_data_dirs=data_dirs,
        zotero_discovery_roots=discovery_roots,
        zotero_discovery_max_depth=discovery_max_depth,
        zotero_path_prefix_map=env_string_prefix_map("ZOTERO_PATH_PREFIX_MAP"),
        zotero_storage_dir=(
            env_path("ZOTERO_STORAGE_DIR", Path())
            if os.environ.get("ZOTERO_STORAGE_DIR")
            else None
        ),
        worker_host=os.environ.get("ZOTERO_INGEST_HOST", os.environ.get("ZOTERO_WORKER_HOST", "0.0.0.0")),
        worker_port=env_int("ZOTERO_INGEST_PORT", env_int("ZOTERO_WORKER_PORT", 8765)),
        worker_token=os.environ.get("ZOTERO_INGEST_TOKEN", os.environ.get("ZOTERO_WORKER_TOKEN", "")),
        worker_role=normalize_worker_role(os.environ.get("ZOTERO_INGEST_ROLE"), default=ROLE_ALL),
        ingest_data_root=ingest_data_root,
        html_data_root=env_first_path(
            ("INGEST_HTML_ROOT", "HTML_DATA_ROOT"),
            PROJECT_ROOT / "data" / "html",
        ),
        zotero_parent_preflight_enabled=env_bool("ZOTERO_PARENT_PREFLIGHT_ENABLED", True),
        metadata_crossref_email=os.environ.get("METADATA_CROSSREF_EMAIL", "").strip(),
        metadata_unpaywall_email=os.environ.get("METADATA_UNPAYWALL_EMAIL", "").strip(),
        metadata_openalex_api_key=os.environ.get("METADATA_OPENALEX_API_KEY", "").strip(),
        metadata_semantic_scholar_api_key=os.environ.get(
            "METADATA_SEMANTIC_SCHOLAR_API_KEY",
            "",
        ).strip(),
        metadata_core_api_key=os.environ.get("METADATA_CORE_API_KEY", "").strip(),
        metadata_extended_providers_enabled=env_bool(
            "METADATA_EXTENDED_PROVIDERS_ENABLED",
            True,
        ),
        metadata_request_timeout_seconds=env_int("METADATA_REQUEST_TIMEOUT_SECONDS", 60),
        metadata_user_agent=os.environ.get(
            "METADATA_USER_AGENT",
            "zotero-ingest-worker/0.1 (mailto:metadata@example.local)",
        ),
        metadata_policy=os.environ.get("METADATA_PATCH_POLICY", "emptyFieldsOnly").strip()
        or "emptyFieldsOnly",
        metadata_title_min_score=env_float("METADATA_TITLE_MIN_SCORE", 0.86),
        metadata_job_lease_seconds=env_int("METADATA_JOB_LEASE_SECONDS", 900),
        zotero_translation_server_url=os.environ.get("ZOTERO_TRANSLATION_SERVER_URL", "").rstrip("/"),
        zotero_translation_server_timeout_seconds=env_int(
            "ZOTERO_TRANSLATION_SERVER_TIMEOUT_SECONDS",
            60,
        ),
        arxiv_html_root=env_path(
            "ARXIV_HTML_ROOT",
            PROJECT_ROOT / "data" / "html" / "arxiv",
        ),
        arxiv_html_attach=env_bool("ARXIV_HTML_ATTACH", True),
        arxiv_html_fetch_timeout_seconds=env_int("ARXIV_HTML_FETCH_TIMEOUT_SECONDS", 120),
        arxiv_html_min_text_chars=env_int("ARXIV_HTML_MIN_TEXT_CHARS", 200),
        arxiv_search_min_score=env_float("ARXIV_SEARCH_MIN_SCORE", 0.88),
        scihub_enabled=env_bool("SCIHUB_ENABLED", True),
        scihub_base_url=scihub_base_url,
        scihub_mirrors=scihub_mirrors,
        scihub_user_agent=os.environ.get(
            "SCIHUB_USER_AGENT",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 11_3_1 like Mac OS X) "
            "AppleWebKit/603.1.30 (KHTML, like Gecko) Version/10.0 Mobile/14E304 Safari/602.1",
        ),
        scihub_request_timeout_seconds=env_int("SCIHUB_REQUEST_TIMEOUT_SECONDS", 60),
        pdf_text_min_chars=env_int("PDF_TEXT_MIN_CHARS", 80),
        pdf_text_check_pages=env_int("PDF_TEXT_CHECK_PAGES", 5),
        scan_limit=env_int("ZOTERO_SCAN_LIMIT", 5),
        scan_max_items=env_int("ZOTERO_SCAN_MAX_ITEMS", 50),
        retry_failed=env_bool("ZOTERO_RETRY_FAILED", False),
        request_timeout_seconds=env_int("ZOTERO_REQUEST_TIMEOUT_SECONDS", 300),
        zotero_relay_url=os.environ.get("ZOTERO_RELAY_URL", "").rstrip("/"),
        zotero_relay_token=(
            os.environ.get("ZOTERO_RELAY_TOKEN")
            or os.environ.get("ZFR_TOKEN")
            or ""
        ),
        zotero_relay_replace_strategy=os.environ.get("ZOTERO_RELAY_REPLACE_STRATEGY", ""),
    )


def apply_request_overrides(config: WorkerConfig, payload: dict[str, Any]) -> WorkerConfig:
    updates: dict[str, Any] = {}

    if "limit" in payload and payload["limit"] is not None:
        updates["scan_limit"] = int(payload["limit"])
    if "max_items" in payload and payload["max_items"] is not None:
        updates["scan_max_items"] = int(payload["max_items"])

    zotero = payload.get("zotero") or {}
    if isinstance(zotero, dict):
        if "relay_strategy" in zotero:
            updates["zotero_relay_replace_strategy"] = str(zotero["relay_strategy"] or "")
        if "relay_url" in zotero:
            updates["zotero_relay_url"] = str(zotero["relay_url"] or "").rstrip("/")

    if "retry_failed" in payload:
        updates["retry_failed"] = bool(payload["retry_failed"])

    return replace(config, **updates) if updates else config


def _normalize_prefix_path(value: str) -> str:
    return value.strip().replace("\\", "/").rstrip("/")
