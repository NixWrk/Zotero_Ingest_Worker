from __future__ import annotations

import json
import math
import os
import urllib.parse
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .bounded_io import read_text_bounded
from .worker_roles import ROLE_ALL, normalize_worker_role


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_DISCOVERY_DEPTH = 32
MAX_OPERATION_ITEMS = 1_000_000
MAX_WORKERS = 256
MAX_TIMEOUT_SECONDS = 86_400
MAX_JOB_LEASE_SECONDS = 604_800
MAX_TEXT_THRESHOLD = 10_000_000
MAX_PDF_CHECK_PAGES = 1_000
MAX_LIBRARY_BINDINGS = 1_024
MAX_PREFIX_MAPPINGS = 128
MAX_CONFIG_PATH_CHARS = 4_096
MAX_CONFIG_PATH_ENTRIES = 1_024
MAX_DISCOVERY_DIRECTORIES = 100_000
MAX_CONFIG_URL_CHARS = 2_048
MAX_SCIHUB_MIRRORS = 32
MAX_DOTENV_BYTES = 1_000_000
MAX_DOTENV_LINE_CHARS = 16_384
MAX_CONFIG_HOST_CHARS = 253
MAX_CONFIG_HEADER_CHARS = 1_024
MAX_CONFIG_TOKEN_CHARS = 8_192
MAX_CONFIG_ROLE_CHARS = 64
METADATA_POLICIES = frozenset({"emptyFieldsOnly", "allowOverwrite"})


def load_dotenv(path: Path | None = None) -> None:
    env_path = path or PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    if not env_path.is_file():
        raise ValueError(f"Invalid .env path: expected a file: {env_path}")
    try:
        size = env_path.stat().st_size
    except OSError as exc:
        raise ValueError(f"Could not inspect .env file: {env_path}") from exc
    if size > MAX_DOTENV_BYTES:
        raise ValueError(
            f"Invalid .env file: {env_path} exceeds {MAX_DOTENV_BYTES} bytes."
        )
    try:
        text = read_text_bounded(
            env_path,
            max_bytes=MAX_DOTENV_BYTES,
            encoding="utf-8",
        )
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Could not read UTF-8 .env file: {env_path}") from exc
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if len(raw_line) > MAX_DOTENV_LINE_CHARS:
            raise ValueError(
                f"Invalid .env assignment at {env_path}:{line_number}: line is too long."
            )
        line = raw_line.strip()
        if line_number == 1:
            line = line.lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(
                f"Invalid .env assignment at {env_path}:{line_number}: expected KEY=VALUE."
            )
        key, value = line.split("=", 1)
        key = key.strip()
        if not _is_valid_env_key(key):
            raise ValueError(f"Invalid .env key at {env_path}:{line_number}: {key!r}.")
        value = value.strip()
        if value[:1] in {'"', "'"}:
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise ValueError(
                    f"Invalid .env quoted value at {env_path}:{line_number}."
                )
            value = value[1:-1]
        elif value[-1:] in {'"', "'"}:
            raise ValueError(f"Invalid .env quoted value at {env_path}:{line_number}.")
        if "\x00" in value:
            raise ValueError(
                f"Invalid .env value at {env_path}:{line_number}: NUL is forbidden."
            )
        os.environ.setdefault(key, value)


def _is_valid_env_key(value: str) -> bool:
    if not value or (not value[0].isalpha() and value[0] != "_"):
        return False
    return all(
        character in "_0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        for character in value
    )


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    text = _strip_config_value(value, field=name)
    if not text:
        return default
    normalized = text.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be one of: 1/0, true/false, yes/no, on/off.")


def env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = os.environ.get(name)
    if value is None:
        parsed = default
    else:
        text = _strip_config_value(value, field=name)
        if not text:
            return _bounded_integer(
                default,
                field=name,
                minimum=minimum,
                maximum=maximum,
            )
        digits = text[1:] if text[:1] in {"+", "-"} else text
        if not digits or any(character not in "0123456789" for character in digits):
            raise ValueError(f"{name} must be a base-10 integer.")
        parsed = int(text, 10)
    return _bounded_integer(
        parsed,
        field=name,
        minimum=minimum,
        maximum=maximum,
    )


def env_first_int(
    names: tuple[str, ...],
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    for name in names:
        value = os.environ.get(name)
        if value is None:
            continue
        if _strip_config_value(value, field=name):
            return env_int(
                name,
                default,
                minimum=minimum,
                maximum=maximum,
            )
    return _bounded_integer(
        default,
        field=names[0],
        minimum=minimum,
        maximum=maximum,
    )


def env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = os.environ.get(name)
    if value is None:
        parsed = default
    else:
        text = _strip_config_value(value, field=name)
        if not text:
            parsed = default
        else:
            if any(character not in "+-.0123456789eE" for character in text):
                raise ValueError(f"{name} must be a finite number.")
            try:
                parsed = float(text)
            except ValueError as exc:
                raise ValueError(f"{name} must be a finite number.") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be a finite number.")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{name} must be at most {maximum}.")
    return parsed


def _bounded_integer(
    value: int,
    *,
    field: str,
    minimum: int | None,
    maximum: int | None,
) -> int:
    if minimum is not None and value < minimum:
        requirement = "be non-negative" if minimum == 0 else f"be at least {minimum}"
        raise ValueError(f"{field} must {requirement}.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field} must be at most {maximum}.")
    return value


def _has_control_character(value: str) -> bool:
    return any(not character.isprintable() for character in value)


def _strip_config_value(value: str, *, field: str) -> str:
    if _has_control_character(value):
        raise ValueError(f"{field} contains forbidden control characters.")
    return value.strip()


def _bounded_path_text(value: str, *, field: str) -> str:
    text = _strip_config_value(value, field=field)
    if len(text) > MAX_CONFIG_PATH_CHARS:
        raise ValueError(f"{field} exceeds {MAX_CONFIG_PATH_CHARS} characters.")
    return text


def env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if value is None:
        return default.expanduser()
    text = _bounded_path_text(value, field=name)
    return Path(text).expanduser() if text else default.expanduser()


def env_first_path(names: tuple[str, ...], default: Path) -> Path:
    for name in names:
        value = os.environ.get(name)
        if value is None:
            continue
        text = _bounded_path_text(value, field=name)
        if text:
            return Path(text).expanduser()
    return default.expanduser()


def env_optional_path(name: str) -> Path | None:
    value = os.environ.get(name)
    if value is None:
        return None
    text = _bounded_path_text(value, field=name)
    return Path(text).expanduser() if text else None


def env_path_list(name: str) -> tuple[Path, ...]:
    value = os.environ.get(name)
    if value is None:
        return ()
    parts = value.split(";")
    if len(parts) > MAX_CONFIG_PATH_ENTRIES:
        raise ValueError(
            f"{name} allows at most {MAX_CONFIG_PATH_ENTRIES} path entries."
        )
    paths: list[Path] = []
    for index, part in enumerate(parts):
        text = _bounded_path_text(part, field=f"{name}[{index}]")
        if text:
            paths.append(Path(text).expanduser())
    return tuple(paths)


def env_zfr_library_data_dirs(
    *,
    path_prefix_map: tuple[tuple[str, str], ...] = (),
) -> tuple[Path, ...]:
    value = os.environ.get("ZFR_LIBRARY_BINDINGS", "").strip()
    if not value:
        return ()
    try:
        bindings = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid ZFR_LIBRARY_BINDINGS JSON.") from exc
    if not isinstance(bindings, list):
        raise ValueError("Invalid ZFR_LIBRARY_BINDINGS: expected a JSON list.")
    if len(bindings) > MAX_LIBRARY_BINDINGS:
        raise ValueError(
            f"Invalid ZFR_LIBRARY_BINDINGS: at most {MAX_LIBRARY_BINDINGS} bindings are allowed."
        )

    result: list[Path] = []
    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict):
            raise ValueError(
                f"Invalid ZFR_LIBRARY_BINDINGS[{index}]: expected a JSON object."
            )
        candidates: list[Path] = []
        data_dir = _binding_path_value(binding, index=index, field="dataDir")
        host_data_dir = _binding_path_value(binding, index=index, field="hostDataDir")
        if data_dir:
            candidates.append(Path(data_dir).expanduser())
        if host_data_dir:
            candidates.append(_translate_prefix_path(host_data_dir, path_prefix_map))
            candidates.append(Path(host_data_dir).expanduser())
        for candidate in candidates:
            expanded = candidate.expanduser()
            if is_zotero_data_dir(expanded):
                result.append(expanded)
                break
    return unique_paths(tuple(result))


def env_string_prefix_map(name: str) -> tuple[tuple[str, str], ...]:
    value = os.environ.get(name)
    if value is None:
        return ()
    value = _strip_config_value(value, field=name)
    if not value:
        return ()
    result: list[tuple[str, str]] = []
    seen_sources: dict[str, str] = {}
    for raw_part in value.split(";"):
        part = raw_part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(
                f"Invalid {name} entry: {part!r}. Expected source_prefix=target_prefix."
            )
        source_prefix, target_prefix = part.split("=", 1)
        source_prefix = _bounded_path_text(source_prefix, field=name)
        target_prefix = _bounded_path_text(target_prefix, field=name)
        normalized_source = _normalize_prefix_path(source_prefix)
        normalized_target = _normalize_prefix_path(target_prefix)
        if not normalized_source or not normalized_target:
            raise ValueError(
                f"Invalid {name} entry: {part!r}. Expected source_prefix=target_prefix."
            )
        source_key = _prefix_comparison_key(source_prefix)
        target_key = os.path.normcase(
            os.path.normpath(str(Path(target_prefix).expanduser()))
        )
        existing_target = seen_sources.get(source_key)
        if existing_target is not None:
            if existing_target != target_key:
                raise ValueError(
                    f"Invalid {name}: normalized source prefix {source_prefix!r} has conflicting targets."
                )
            continue
        if len(result) >= MAX_PREFIX_MAPPINGS:
            raise ValueError(
                f"Invalid {name}: at most {MAX_PREFIX_MAPPINGS} mappings are allowed."
            )
        seen_sources[source_key] = target_key
        result.append((source_prefix, target_prefix))
    result.sort(key=lambda item: len(_normalize_prefix_path(item[0])), reverse=True)
    return tuple(result)


def unique_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        expanded = path.expanduser()
        normalized = os.path.normcase(os.path.normpath(str(expanded)))
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(expanded)
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


def _directory_identity(path: Path) -> tuple[str, int, int] | tuple[str, str]:
    stat = path.stat()
    if stat.st_ino > 0:
        return ("inode", stat.st_dev, stat.st_ino)
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = path.absolute()
    normalized = os.path.normcase(os.path.normpath(str(resolved)))
    return ("path", normalized)


def discover_zotero_data_dirs(
    roots: tuple[Path, ...],
    *,
    max_depth: int,
) -> tuple[Path, ...]:
    if type(max_depth) is not int or max_depth < 0 or max_depth > MAX_DISCOVERY_DEPTH:
        raise ValueError(
            f"max_depth must be an integer between 0 and {MAX_DISCOVERY_DEPTH}."
        )
    found: list[Path] = []
    visited: set[tuple[str, int, int] | tuple[str, str]] = set()
    for root in roots:
        expanded = root.expanduser()
        if not expanded.exists():
            continue
        stack: list[tuple[Path, int]] = [(expanded, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                identity = _directory_identity(current)
            except (OSError, ValueError):
                continue
            if identity in visited:
                continue
            visited.add(identity)
            if len(visited) > MAX_DISCOVERY_DIRECTORIES:
                raise RuntimeError(
                    f"Zotero discovery exceeded {MAX_DISCOVERY_DIRECTORIES} directories."
                )
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
    full_text_attach_raw_html_fallback: bool

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
    metadata_drain_max_workers: int
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
        return _translate_prefix_path(raw, self.zotero_path_prefix_map)

    def validate_for_scan(self) -> None:
        for data_dir in self.zotero_data_dirs:
            if not data_dir.is_dir():
                raise ValueError(f"Zotero data directory does not exist: {data_dir}")
            if not (data_dir / "zotero.sqlite").is_file():
                raise ValueError(
                    f"zotero.sqlite was not found: {data_dir / 'zotero.sqlite'}"
                )
            if not (data_dir / "storage").is_dir():
                raise ValueError(
                    f"Zotero storage directory does not exist: {data_dir / 'storage'}"
                )


DEFAULT_SCIHUB_MIRRORS = (
    "https://sci-hub.ru/",
    "https://sci-hub.se/",
    "https://sci-hub.st/",
)


def parse_scihub_mirrors(raw: str, *, base_url: str) -> tuple[str, ...]:
    if not isinstance(raw, str):
        raise ValueError("SCIHUB_MIRRORS must be a string.")
    normalized_base = _normalize_http_base_url(
        base_url,
        field="SCIHUB_BASE_URL",
        allow_empty=False,
        trailing_slash=True,
    )
    entries = [part.strip() for part in raw.split(";") if part.strip()]
    if len(entries) > MAX_SCIHUB_MIRRORS:
        raise ValueError(f"SCIHUB_MIRRORS allows at most {MAX_SCIHUB_MIRRORS} entries.")
    if not entries:
        entries = [normalized_base, *DEFAULT_SCIHUB_MIRRORS]
    else:
        entries = [normalized_base, *entries]
    seen: set[str] = set()
    mirrors: list[str] = []
    for index, entry in enumerate(entries):
        normalized = _normalize_http_base_url(
            entry,
            field=("SCIHUB_BASE_URL" if index == 0 else "SCIHUB_MIRRORS"),
            allow_empty=False,
            trailing_slash=True,
        )
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
    discovery_roots = (
        env_path_list("ZOTERO_DISCOVERY_ROOTS") or default_discovery_roots()
    )
    discovery_max_depth = env_int(
        "ZOTERO_DISCOVERY_MAX_DEPTH",
        4,
        minimum=0,
        maximum=MAX_DISCOVERY_DEPTH,
    )
    auto_discover = env_bool("ZOTERO_AUTO_DISCOVER", True)
    zotero_path_prefix_map = env_string_prefix_map("ZOTERO_PATH_PREFIX_MAP")
    binding_data_dirs = env_zfr_library_data_dirs(
        path_prefix_map=zotero_path_prefix_map
    )
    discovered_data_dirs = (
        discover_zotero_data_dirs(discovery_roots, max_depth=discovery_max_depth)
        if auto_discover
        else ()
    )
    if explicit_data_dirs:
        data_dirs = unique_paths(explicit_data_dirs)
    elif binding_data_dirs:
        data_dirs = binding_data_dirs
    else:
        data_dirs = discovered_data_dirs
    if not data_dirs and is_zotero_data_dir(primary_data_dir):
        data_dirs = (primary_data_dir,)
    if not data_dirs:
        data_dirs = (primary_data_dir,)

    scihub_base_url = _normalize_http_base_url(
        os.environ.get("SCIHUB_BASE_URL", "https://sci-hub.ru/"),
        field="SCIHUB_BASE_URL",
        allow_empty=False,
        trailing_slash=True,
    )
    scihub_mirrors = parse_scihub_mirrors(
        os.environ.get("SCIHUB_MIRRORS", ""), base_url=scihub_base_url
    )
    ingest_data_root = env_first_path(
        ("INGEST_DATA_ROOT", "ZOTERO_INGEST_DATA_ROOT", "OCR_DATA_ROOT"),
        PROJECT_ROOT / "data" / "ingest",
    )

    return WorkerConfig(
        zotero_data_dir=data_dirs[0],
        zotero_data_dirs=data_dirs,
        zotero_discovery_roots=discovery_roots,
        zotero_discovery_max_depth=discovery_max_depth,
        zotero_path_prefix_map=zotero_path_prefix_map,
        zotero_storage_dir=env_optional_path("ZOTERO_STORAGE_DIR"),
        worker_host=_env_bind_host(
            ("ZOTERO_INGEST_HOST", "ZOTERO_WORKER_HOST"),
            default="0.0.0.0",
        ),
        worker_port=env_first_int(
            ("ZOTERO_INGEST_PORT", "ZOTERO_WORKER_PORT"),
            8765,
            minimum=1,
            maximum=65_535,
        ),
        worker_token=_env_auth_token(
            ("ZOTERO_INGEST_TOKEN", "ZOTERO_WORKER_TOKEN"),
            skip_blank=False,
        ),
        worker_role=_env_worker_role("ZOTERO_INGEST_ROLE", default=ROLE_ALL),
        ingest_data_root=ingest_data_root,
        html_data_root=env_first_path(
            ("INGEST_HTML_ROOT", "HTML_DATA_ROOT"),
            PROJECT_ROOT / "data" / "html",
        ),
        zotero_parent_preflight_enabled=env_bool(
            "ZOTERO_PARENT_PREFLIGHT_ENABLED", True
        ),
        full_text_attach_raw_html_fallback=env_bool(
            "FULL_TEXT_ATTACH_RAW_HTML_FALLBACK", False
        ),
        metadata_crossref_email=_env_auth_token(
            ("METADATA_CROSSREF_EMAIL",), skip_blank=False
        ),
        metadata_unpaywall_email=_env_auth_token(
            ("METADATA_UNPAYWALL_EMAIL",), skip_blank=False
        ),
        metadata_openalex_api_key=_env_auth_token(
            ("METADATA_OPENALEX_API_KEY",), skip_blank=False
        ),
        metadata_semantic_scholar_api_key=_env_auth_token(
            ("METADATA_SEMANTIC_SCHOLAR_API_KEY",), skip_blank=False
        ),
        metadata_core_api_key=_env_auth_token(
            ("METADATA_CORE_API_KEY",), skip_blank=False
        ),
        metadata_extended_providers_enabled=env_bool(
            "METADATA_EXTENDED_PROVIDERS_ENABLED",
            True,
        ),
        metadata_request_timeout_seconds=env_int(
            "METADATA_REQUEST_TIMEOUT_SECONDS",
            60,
            minimum=1,
            maximum=MAX_TIMEOUT_SECONDS,
        ),
        metadata_user_agent=_env_header_value(
            "METADATA_USER_AGENT",
            "zotero-ingest-worker/0.1 (mailto:metadata@example.local)",
        ),
        metadata_policy=_env_choice(
            "METADATA_PATCH_POLICY",
            "emptyFieldsOnly",
            choices=METADATA_POLICIES,
        ),
        metadata_title_min_score=env_float(
            "METADATA_TITLE_MIN_SCORE",
            0.86,
            minimum=0.0,
            maximum=1.0,
        ),
        metadata_job_lease_seconds=env_int(
            "METADATA_JOB_LEASE_SECONDS",
            900,
            minimum=60,
            maximum=MAX_JOB_LEASE_SECONDS,
        ),
        metadata_drain_max_workers=env_int(
            "METADATA_DRAIN_MAX_WORKERS",
            1,
            minimum=1,
            maximum=MAX_WORKERS,
        ),
        zotero_translation_server_url=_normalize_http_base_url(
            os.environ.get("ZOTERO_TRANSLATION_SERVER_URL", ""),
            field="ZOTERO_TRANSLATION_SERVER_URL",
            allow_empty=True,
            trailing_slash=False,
        ),
        zotero_translation_server_timeout_seconds=env_int(
            "ZOTERO_TRANSLATION_SERVER_TIMEOUT_SECONDS",
            60,
            minimum=1,
            maximum=MAX_TIMEOUT_SECONDS,
        ),
        arxiv_html_root=env_path(
            "ARXIV_HTML_ROOT",
            PROJECT_ROOT / "data" / "html" / "arxiv",
        ),
        arxiv_html_attach=env_bool("ARXIV_HTML_ATTACH", True),
        arxiv_html_fetch_timeout_seconds=env_int(
            "ARXIV_HTML_FETCH_TIMEOUT_SECONDS",
            120,
            minimum=1,
            maximum=MAX_TIMEOUT_SECONDS,
        ),
        arxiv_html_min_text_chars=env_int(
            "ARXIV_HTML_MIN_TEXT_CHARS",
            200,
            minimum=1,
            maximum=MAX_TEXT_THRESHOLD,
        ),
        arxiv_search_min_score=env_float(
            "ARXIV_SEARCH_MIN_SCORE",
            0.88,
            minimum=0.0,
            maximum=1.0,
        ),
        scihub_enabled=env_bool("SCIHUB_ENABLED", True),
        scihub_base_url=scihub_base_url,
        scihub_mirrors=scihub_mirrors,
        scihub_user_agent=_env_header_value(
            "SCIHUB_USER_AGENT",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 11_3_1 like Mac OS X) "
            "AppleWebKit/603.1.30 (KHTML, like Gecko) Version/10.0 Mobile/14E304 Safari/602.1",
        ),
        scihub_request_timeout_seconds=env_int(
            "SCIHUB_REQUEST_TIMEOUT_SECONDS",
            60,
            minimum=1,
            maximum=MAX_TIMEOUT_SECONDS,
        ),
        pdf_text_min_chars=env_int(
            "PDF_TEXT_MIN_CHARS",
            80,
            minimum=1,
            maximum=MAX_TEXT_THRESHOLD,
        ),
        pdf_text_check_pages=env_int(
            "PDF_TEXT_CHECK_PAGES",
            5,
            minimum=1,
            maximum=MAX_PDF_CHECK_PAGES,
        ),
        scan_limit=env_int(
            "ZOTERO_SCAN_LIMIT",
            5,
            minimum=0,
            maximum=MAX_OPERATION_ITEMS,
        ),
        scan_max_items=env_int(
            "ZOTERO_SCAN_MAX_ITEMS",
            50,
            minimum=0,
            maximum=MAX_OPERATION_ITEMS,
        ),
        retry_failed=env_bool("ZOTERO_RETRY_FAILED", False),
        request_timeout_seconds=env_int(
            "ZOTERO_REQUEST_TIMEOUT_SECONDS",
            300,
            minimum=1,
            maximum=MAX_TIMEOUT_SECONDS,
        ),
        zotero_relay_url=_normalize_http_base_url(
            os.environ.get("ZOTERO_RELAY_URL", ""),
            field="ZOTERO_RELAY_URL",
            allow_empty=True,
            trailing_slash=False,
        ),
        zotero_relay_token=_env_auth_token(
            ("ZOTERO_RELAY_TOKEN", "ZFR_TOKEN"),
            skip_blank=True,
        ),
        zotero_relay_replace_strategy=_normalize_strategy(
            os.environ.get("ZOTERO_RELAY_REPLACE_STRATEGY", ""),
            field="ZOTERO_RELAY_REPLACE_STRATEGY",
        ),
    )


def apply_request_overrides(
    config: WorkerConfig, payload: dict[str, Any]
) -> WorkerConfig:
    updates: dict[str, Any] = {}

    if payload.get("workers") is not None and payload.get("max_workers") is not None:
        raise ValueError("workers and max_workers cannot be provided together.")
    if "limit" in payload and payload["limit"] is not None:
        updates["scan_limit"] = _exact_request_integer(
            payload["limit"],
            field="limit",
            minimum=0,
            maximum=MAX_OPERATION_ITEMS,
        )
    if "max_items" in payload and payload["max_items"] is not None:
        updates["scan_max_items"] = _exact_request_integer(
            payload["max_items"],
            field="max_items",
            minimum=0,
            maximum=MAX_OPERATION_ITEMS,
        )
    if "workers" in payload and payload["workers"] is not None:
        updates["metadata_drain_max_workers"] = _exact_request_integer(
            payload["workers"],
            field="workers",
            minimum=1,
            maximum=MAX_WORKERS,
        )
    if "max_workers" in payload and payload["max_workers"] is not None:
        updates["metadata_drain_max_workers"] = _exact_request_integer(
            payload["max_workers"],
            field="max_workers",
            minimum=1,
            maximum=MAX_WORKERS,
        )

    zotero_value = payload.get("zotero")
    if zotero_value is None:
        zotero: dict[str, Any] = {}
    elif not isinstance(zotero_value, dict):
        raise ValueError("zotero must be a JSON object or null.")
    else:
        zotero = zotero_value
    if "relay_strategy" in zotero:
        updates["zotero_relay_replace_strategy"] = _normalize_strategy(
            _nullable_request_string(
                zotero["relay_strategy"], field="zotero.relay_strategy"
            ),
            field="zotero.relay_strategy",
        )
    if "relay_url" in zotero:
        updates["zotero_relay_url"] = _normalize_http_base_url(
            _nullable_request_string(zotero["relay_url"], field="zotero.relay_url"),
            field="zotero.relay_url",
            allow_empty=True,
            trailing_slash=False,
        )

    if "retry_failed" in payload:
        retry_failed = payload["retry_failed"]
        if not isinstance(retry_failed, bool):
            raise ValueError("retry_failed must be a JSON boolean.")
        updates["retry_failed"] = retry_failed

    return replace(config, **updates) if updates else config


def _exact_request_integer(
    value: object,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a JSON integer.")
    return _bounded_integer(
        value,
        field=field,
        minimum=minimum,
        maximum=maximum,
    )


def _nullable_request_string(value: object, *, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a JSON string or null.")
    return value


def _binding_path_value(
    binding: dict[str, Any],
    *,
    index: int,
    field: str,
) -> str:
    value = binding.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(
            f"Invalid ZFR_LIBRARY_BINDINGS[{index}].{field}: expected a JSON string or null."
        )
    try:
        return _bounded_path_text(
            value,
            field=f"ZFR_LIBRARY_BINDINGS[{index}].{field}",
        )
    except ValueError as exc:
        raise ValueError(
            f"Invalid ZFR_LIBRARY_BINDINGS[{index}].{field}: {exc}"
        ) from exc


def _env_choice(
    name: str,
    default: str,
    *,
    choices: frozenset[str],
) -> str:
    value = os.environ.get(name)
    selected = _strip_config_value(value, field=name) if value is not None else default
    if not selected:
        selected = default
    if selected not in choices:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(choices))}.")
    return selected


def _normalize_strategy(value: str, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string.")
    text = _strip_config_value(value, field=field).casefold()
    if not text:
        return ""
    if len(text) > 64 or any(
        character not in "-_0123456789abcdefghijklmnopqrstuvwxyz" for character in text
    ):
        raise ValueError(
            f"{field} must contain only ASCII letters, digits, '-' or '_'."
        )
    return text


def _validate_url_percent_escapes(value: str, *, field: str) -> None:
    hexadecimal = frozenset("0123456789abcdefABCDEF")
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if (
            index + 2 >= len(value)
            or value[index + 1] not in hexadecimal
            or value[index + 2] not in hexadecimal
        ):
            raise ValueError(f"{field} contains a malformed percent escape.")
        decoded = int(value[index + 1 : index + 3], 16)
        if decoded < 32 or decoded in {92, 127}:
            raise ValueError(f"{field} contains an encoded control or backslash.")
        index += 3


def _normalize_http_base_url(
    value: str,
    *,
    field: str,
    allow_empty: bool,
    trailing_slash: bool,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string.")
    text = _strip_config_value(value, field=field)
    if not text:
        if allow_empty:
            return ""
        raise ValueError(f"{field} must be a non-empty HTTP(S) URL.")
    if len(text) > MAX_CONFIG_URL_CHARS:
        raise ValueError(f"{field} exceeds {MAX_CONFIG_URL_CHARS} characters.")
    if "\\" in text or any(character.isspace() for character in text):
        raise ValueError(f"{field} contains forbidden whitespace or controls.")
    _validate_url_percent_escapes(text, field=field)
    try:
        parsed = urllib.parse.urlsplit(text)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid HTTP(S) URL.") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not hostname:
        raise ValueError(f"{field} must use HTTP(S) and include a host.")
    authority = parsed.netloc.rsplit("@", 1)[-1]
    if authority.endswith(":"):
        raise ValueError(f"{field} contains an empty port.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field} must not contain credentials.")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field} must not contain a query or fragment.")
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError(f"{field} contains an invalid port.")
    normalized = urllib.parse.urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc, parsed.path.rstrip("/"), "", "")
    )
    return f"{normalized}/" if trailing_slash else normalized


def _env_worker_role(name: str, *, default: str) -> str:
    value = os.environ.get(name)
    text = _strip_config_value(value, field=name) if value is not None else default
    if len(text) > MAX_CONFIG_ROLE_CHARS:
        raise ValueError(f"{name} exceeds {MAX_CONFIG_ROLE_CHARS} characters.")
    return normalize_worker_role(text or default, default=default)


def _env_bind_host(names: tuple[str, ...], *, default: str) -> str:
    field = names[0]
    text = default
    for name in names:
        value = os.environ.get(name)
        if value is None:
            continue
        candidate = _strip_config_value(value, field=name)
        if candidate:
            field = name
            text = candidate
            break
    if (
        not text
        or len(text) > MAX_CONFIG_HOST_CHARS
        or not text.isascii()
        or any(character.isspace() for character in text)
        or "/" in text
        or "\\" in text
    ):
        raise ValueError(f"{field} must be a non-empty host without whitespace.")
    return text


def _env_header_value(name: str, default: str) -> str:
    value = os.environ.get(name)
    text = _strip_config_value(value, field=name) if value is not None else default
    if not text:
        text = default
    if len(text) > MAX_CONFIG_HEADER_CHARS or not text.isascii():
        raise ValueError(
            f"{name} must be ASCII and at most {MAX_CONFIG_HEADER_CHARS} characters."
        )
    return text


def _env_auth_token(
    names: tuple[str, ...],
    *,
    skip_blank: bool,
) -> str:
    for name in names:
        if name not in os.environ:
            continue
        text = _normalize_auth_token(os.environ[name], field=name)
        if skip_blank and not text:
            continue
        return text
    return ""


def _normalize_auth_token(value: str, *, field: str) -> str:
    text = _strip_config_value(value, field=field)
    if (
        len(text) > MAX_CONFIG_TOKEN_CHARS
        or not text.isascii()
        or any(character.isspace() for character in text)
    ):
        raise ValueError(f"{field} must not contain whitespace.")
    return text


def _normalize_prefix_path(value: str) -> str:
    normalized = str(value).strip().replace("\\", "/")
    if normalized == "/" or (
        len(normalized) == 3 and normalized[0].isalpha() and normalized[1:] == ":/"
    ):
        return normalized
    return normalized.rstrip("/")


def _is_windows_style_path(value: str) -> bool:
    return (
        len(value) >= 2 and value[0].isalpha() and value[1] == ":"
    ) or value.startswith("//")


def _prefix_comparison_key(value: str) -> str:
    normalized = _normalize_prefix_path(value)
    return (
        normalized.lower()
        if _is_windows_style_path(normalized) or os.name == "nt"
        else normalized
    )


def _translate_prefix_path(
    raw_path: str,
    path_prefix_map: tuple[tuple[str, str], ...],
) -> Path:
    normalized_raw = _normalize_prefix_path(raw_path)
    best_match: tuple[int, str, str] | None = None
    for source_prefix, target_prefix in path_prefix_map:
        normalized_source = _normalize_prefix_path(source_prefix)
        case_insensitive = _is_windows_style_path(normalized_source) or os.name == "nt"
        raw_key = normalized_raw.lower() if case_insensitive else normalized_raw
        source_key = (
            normalized_source.lower() if case_insensitive else normalized_source
        )
        source_boundary = source_key if source_key.endswith("/") else f"{source_key}/"
        if raw_key != source_key and not raw_key.startswith(source_boundary):
            continue
        score = len(normalized_source)
        if best_match is None or score > best_match[0]:
            best_match = (score, normalized_source, str(target_prefix))
    if best_match is None:
        return Path(raw_path).expanduser()
    _, normalized_source, target_prefix = best_match
    relative = normalized_raw[len(normalized_source) :].lstrip("/")
    target = Path(target_prefix).expanduser()
    return target if not relative else target / Path(*relative.split("/"))
