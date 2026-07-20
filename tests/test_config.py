from __future__ import annotations

import os
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
import zotero_ingest_worker.config as config_module

from zotero_ingest_worker.config import (
    apply_request_overrides,
    env_bool,
    env_float,
    env_int,
    from_env,
)


def _write_zotero_data_dir(path: Path) -> None:
    (path / "storage").mkdir(parents=True)
    connection = sqlite3.connect(path / "zotero.sqlite")
    try:
        connection.execute("create table items (itemID integer primary key)")
        connection.commit()
    finally:
        connection.close()


def test_from_env_prefers_relay_library_bindings_over_recursive_discovery(
    monkeypatch,
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "pc_zotero" / "Zotero_Elvis_Data"
    duplicate = tmp_path / "pc_zotero" / "updated" / "Zotero_Elvis_Data"
    _write_zotero_data_dir(canonical)
    _write_zotero_data_dir(duplicate)

    monkeypatch.setenv("ZOTERO_DATA_DIRS", "")
    monkeypatch.setenv("ZOTERO_DISCOVERY_ROOTS", str(tmp_path / "pc_zotero"))
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "1")
    monkeypatch.setenv(
        "ZFR_LIBRARY_BINDINGS",
        json.dumps(
            [
                {
                    "libraryId": "Zotero_Elvis_Data_test",
                    "dataDir": str(canonical),
                    "hostDataDir": str(duplicate),
                }
            ]
        ),
    )

    config = from_env(load_file=False)

    assert config.zotero_data_dirs == (canonical,)
    assert duplicate not in config.zotero_data_dirs


@pytest.mark.parametrize("field", ["limit", "max_items", "workers", "max_workers"])
@pytest.mark.parametrize("malformed", ["7", True, 7.0, [], {}])
def test_apply_request_overrides_requires_exact_integer_values(
    field: str,
    malformed: object,
) -> None:
    config = from_env(load_file=False)

    with pytest.raises(ValueError, match=rf"{field} must be a JSON integer"):
        apply_request_overrides(config, {field: malformed})


@pytest.mark.parametrize("malformed", ["false", 0, 1, None, [], {}])
def test_apply_request_overrides_requires_exact_retry_failed_boolean(
    malformed: object,
) -> None:
    config = from_env(load_file=False)

    with pytest.raises(ValueError, match="retry_failed must be a JSON boolean"):
        apply_request_overrides(config, {"retry_failed": malformed})


def test_apply_request_overrides_accepts_exact_request_types() -> None:
    config = from_env(load_file=False)

    updated = apply_request_overrides(
        config,
        {"limit": 7, "max_items": 8, "workers": 2, "retry_failed": False},
    )

    assert updated.scan_limit == 7
    assert updated.scan_max_items == 8
    assert updated.metadata_drain_max_workers == 2
    assert updated.retry_failed is False


@pytest.mark.parametrize("malformed", ["relay", [], 1, False])
def test_apply_request_overrides_requires_zotero_object(malformed: object) -> None:
    config = from_env(load_file=False)

    with pytest.raises(ValueError, match="zotero must be a JSON object"):
        apply_request_overrides(config, {"zotero": malformed})


@pytest.mark.parametrize("field", ["relay_strategy", "relay_url"])
@pytest.mark.parametrize("malformed", [True, 7, [], {}])
def test_apply_request_overrides_requires_exact_zotero_strings(
    field: str,
    malformed: object,
) -> None:
    config = from_env(load_file=False)

    with pytest.raises(
        ValueError, match=rf"zotero\.{field} must be a JSON string or null"
    ):
        apply_request_overrides(config, {"zotero": {field: malformed}})


def test_apply_request_overrides_accepts_and_clears_zotero_strings() -> None:
    config = from_env(load_file=False)

    updated = apply_request_overrides(
        config,
        {
            "zotero": {
                "relay_strategy": "replace",
                "relay_url": "http://127.0.0.1:23118/",
            }
        },
    )
    assert updated.zotero_relay_replace_strategy == "replace"
    assert updated.zotero_relay_url == "http://127.0.0.1:23118"

    cleared = apply_request_overrides(
        updated,
        {"zotero": {"relay_strategy": None, "relay_url": None}},
    )
    assert cleared.zotero_relay_replace_strategy == ""
    assert cleared.zotero_relay_url == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("NO", False),
        ("off", False),
    ],
)
def test_env_bool_accepts_only_explicit_boolean_tokens(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("TEST_BOOL", raw)

    assert env_bool("TEST_BOOL", not expected) is expected


def test_env_bool_uses_default_for_blank_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_BOOL", "   ")

    assert env_bool("TEST_BOOL", True) is True


@pytest.mark.parametrize("raw", ["2", "treu", "enable", "null"])
def test_env_bool_rejects_ambiguous_values(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv("TEST_BOOL", raw)

    with pytest.raises(ValueError, match="TEST_BOOL"):
        env_bool("TEST_BOOL", True)


@pytest.mark.parametrize("raw", ["1_0", "1.0", "nan", "true"])
def test_env_int_requires_plain_decimal_syntax(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv("TEST_INT", raw)

    with pytest.raises(ValueError, match="TEST_INT"):
        env_int("TEST_INT", 5, minimum=1, maximum=10)


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "-0.01", "1.01"])
def test_env_float_requires_finite_bounded_value(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv("TEST_FLOAT", raw)

    with pytest.raises(ValueError, match="TEST_FLOAT"):
        env_float("TEST_FLOAT", 0.5, minimum=0.0, maximum=1.0)


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("ZOTERO_DISCOVERY_MAX_DEPTH", "-1"),
        ("ZOTERO_DISCOVERY_MAX_DEPTH", "33"),
        ("ZOTERO_INGEST_PORT", "0"),
        ("ZOTERO_INGEST_PORT", "65536"),
        ("METADATA_REQUEST_TIMEOUT_SECONDS", "0"),
        ("METADATA_REQUEST_TIMEOUT_SECONDS", "86401"),
        ("METADATA_TITLE_MIN_SCORE", "nan"),
        ("METADATA_TITLE_MIN_SCORE", "-0.01"),
        ("METADATA_TITLE_MIN_SCORE", "1.01"),
        ("METADATA_JOB_LEASE_SECONDS", "59"),
        ("METADATA_JOB_LEASE_SECONDS", "604801"),
        ("METADATA_DRAIN_MAX_WORKERS", "0"),
        ("METADATA_DRAIN_MAX_WORKERS", "257"),
        ("ZOTERO_TRANSLATION_SERVER_TIMEOUT_SECONDS", "0"),
        ("ARXIV_HTML_FETCH_TIMEOUT_SECONDS", "86401"),
        ("ARXIV_HTML_MIN_TEXT_CHARS", "0"),
        ("ARXIV_SEARCH_MIN_SCORE", "inf"),
        ("SCIHUB_REQUEST_TIMEOUT_SECONDS", "0"),
        ("PDF_TEXT_MIN_CHARS", "10000001"),
        ("PDF_TEXT_CHECK_PAGES", "0"),
        ("PDF_TEXT_CHECK_PAGES", "1001"),
        ("ZOTERO_SCAN_LIMIT", "-1"),
        ("ZOTERO_SCAN_LIMIT", "1000001"),
        ("ZOTERO_SCAN_MAX_ITEMS", "-1"),
        ("ZOTERO_REQUEST_TIMEOUT_SECONDS", "86401"),
    ],
)
def test_from_env_rejects_out_of_range_numeric_configuration(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    raw: str,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv(name, raw)

    with pytest.raises(ValueError, match=name):
        from_env(load_file=False)


def test_primary_worker_port_ignores_malformed_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv("ZOTERO_INGEST_PORT", "8766")
    monkeypatch.setenv("ZOTERO_WORKER_PORT", "not-an-integer")

    assert from_env(load_file=False).worker_port == 8766


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("limit", -1),
        ("limit", 1_000_001),
        ("max_items", -1),
        ("max_items", 1_000_001),
        ("workers", 0),
        ("workers", 257),
        ("max_workers", 0),
        ("max_workers", 257),
    ],
)
def test_apply_request_overrides_rejects_out_of_range_integers(
    field: str,
    value: int,
) -> None:
    config = from_env(load_file=False)

    with pytest.raises(ValueError, match=field):
        apply_request_overrides(config, {field: value})


def test_apply_request_overrides_rejects_ambiguous_worker_aliases() -> None:
    config = from_env(load_file=False)

    with pytest.raises(ValueError, match="workers and max_workers"):
        apply_request_overrides(config, {"workers": 2, "max_workers": 3})


def test_env_path_helpers_ignore_blank_values_and_expand_user(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    default = tmp_path / "default"
    monkeypatch.setenv("TEST_PATH", "   ")
    assert config_module.env_path("TEST_PATH", default) == default

    monkeypatch.setenv("TEST_PATH", "~/zotero-test")
    assert config_module.env_path("TEST_PATH", default) == (Path.home() / "zotero-test")

    monkeypatch.setenv("TEST_PRIMARY_PATH", "   ")
    monkeypatch.setenv("TEST_FALLBACK_PATH", "~/fallback-test")
    assert config_module.env_first_path(
        ("TEST_PRIMARY_PATH", "TEST_FALLBACK_PATH"),
        default,
    ) == (Path.home() / "fallback-test")

    monkeypatch.setenv("TEST_PATHS", "~/one; ;~/two")
    assert config_module.env_path_list("TEST_PATHS") == (
        Path.home() / "one",
        Path.home() / "two",
    )


def test_unique_paths_collapses_syntactic_aliases(tmp_path: Path) -> None:
    canonical = tmp_path / "root"
    alias = canonical / "child" / ".."

    assert config_module.unique_paths((canonical, alias)) == (canonical,)


def test_prefix_translation_uses_longest_windows_match_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    broad = tmp_path / "broad"
    specific = tmp_path / "specific"
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv(
        "ZOTERO_PATH_PREFIX_MAP",
        f"C:\\Data={broad};C:\\Data\\Zotero={specific}",
    )

    config = from_env(load_file=False)

    assert config.translate_zotero_input_path(r"c:\DATA\Zotero\storage\ABC123") == (
        specific / "storage" / "ABC123"
    )


def test_prefix_map_rejects_conflicting_normalized_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TEST_PREFIX_MAP",
        r"C:\Data=/first;c:/data=/second",
    )

    with pytest.raises(ValueError, match="TEST_PREFIX_MAP"):
        config_module.env_string_prefix_map("TEST_PREFIX_MAP")


@pytest.mark.parametrize(
    "bindings",
    [
        [42],
        [{"dataDir": True}],
        [{"hostDataDir": []}],
    ],
)
def test_zfr_library_bindings_reject_malformed_schema(
    monkeypatch: pytest.MonkeyPatch,
    bindings: object,
) -> None:
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", json.dumps(bindings))

    with pytest.raises(ValueError, match="ZFR_LIBRARY_BINDINGS"):
        config_module.env_zfr_library_data_dirs()


def test_zfr_host_path_translation_uses_windows_case_semantics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    _write_zotero_data_dir(canonical)
    monkeypatch.setenv(
        "ZFR_LIBRARY_BINDINGS",
        json.dumps([{"hostDataDir": r"c:\DATA\canonical"}]),
    )

    assert config_module.env_zfr_library_data_dirs(
        path_prefix_map=((r"C:\Data", str(tmp_path)),),
    ) == (canonical,)


@pytest.mark.parametrize("max_depth", [True, -1, 33])
def test_discover_zotero_data_dirs_rejects_invalid_depth(
    tmp_path: Path,
    max_depth: object,
) -> None:
    with pytest.raises(ValueError, match="max_depth"):
        config_module.discover_zotero_data_dirs(
            (tmp_path,),
            max_depth=max_depth,
        )


def test_validate_for_scan_requires_sqlite_file_and_storage_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    malformed = tmp_path / "malformed"
    (malformed / "zotero.sqlite").mkdir(parents=True)
    (malformed / "storage").write_text("not a directory", encoding="utf-8")
    config = replace(
        from_env(load_file=False),
        zotero_data_dir=malformed,
        zotero_data_dirs=(malformed,),
    )

    with pytest.raises(ValueError, match="zotero.sqlite"):
        config.validate_for_scan()


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("ZOTERO_RELAY_URL", "file:///tmp/relay"),
        ("ZOTERO_RELAY_URL", "http://user:secret@relay:23118"),
        ("ZOTERO_RELAY_URL", "http://relay:99999"),
        ("ZOTERO_RELAY_URL", "http://relay/base?token=secret"),
        ("ZOTERO_RELAY_URL", "http://relay/%0Aheader"),
        ("ZOTERO_TRANSLATION_SERVER_URL", "http://translation\\evil"),
        ("SCIHUB_BASE_URL", "javascript:alert(1)"),
        ("SCIHUB_BASE_URL", "https:///missing-host"),
    ],
)
def test_from_env_rejects_ambiguous_or_unsafe_base_urls(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    raw: str,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv(name, raw)

    with pytest.raises(ValueError, match=name):
        from_env(load_file=False)


def test_from_env_normalizes_valid_service_and_mirror_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv("ZOTERO_RELAY_URL", "  http://127.0.0.1:23118///  ")
    monkeypatch.setenv(
        "ZOTERO_TRANSLATION_SERVER_URL",
        "http://translation-server:1969/api/",
    )
    monkeypatch.setenv("SCIHUB_BASE_URL", "https://sci-hub.test/base")
    monkeypatch.setenv(
        "SCIHUB_MIRRORS",
        "https://sci-hub.test/base/;http://mirror.test/root",
    )

    config = from_env(load_file=False)

    assert config.zotero_relay_url == "http://127.0.0.1:23118"
    assert config.zotero_translation_server_url == "http://translation-server:1969/api"
    assert config.scihub_base_url == "https://sci-hub.test/base/"
    assert config.scihub_mirrors == (
        "https://sci-hub.test/base/",
        "http://mirror.test/root/",
    )


@pytest.mark.parametrize("policy", ["overwrite", "allowoverwrite", "unknown"])
def test_from_env_rejects_unknown_metadata_policy(
    monkeypatch: pytest.MonkeyPatch,
    policy: str,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv("METADATA_PATCH_POLICY", policy)

    with pytest.raises(ValueError, match="METADATA_PATCH_POLICY"):
        from_env(load_file=False)


def test_from_env_normalizes_relay_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv("ZOTERO_RELAY_REPLACE_STRATEGY", " LOCAL_THEN_WEBDAV ")

    assert (
        from_env(load_file=False).zotero_relay_replace_strategy == "local_then_webdav"
    )


def test_from_env_rejects_malformed_relay_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv("ZOTERO_RELAY_REPLACE_STRATEGY", "local then webdav")

    with pytest.raises(ValueError, match="ZOTERO_RELAY_REPLACE_STRATEGY"):
        from_env(load_file=False)


@pytest.mark.parametrize(
    "zotero",
    [
        {"relay_url": "file:///tmp/relay"},
        {"relay_url": "http://user:secret@relay"},
        {"relay_strategy": "local then webdav"},
    ],
)
def test_apply_request_overrides_rejects_invalid_relay_configuration(
    zotero: dict[str, str],
) -> None:
    config = from_env(load_file=False)

    with pytest.raises(ValueError, match=r"zotero\.relay"):
        apply_request_overrides(config, {"zotero": zotero})


@pytest.mark.parametrize(
    "content",
    [
        "BAD KEY=value\n",
        "VALID='unterminated\n",
        'VALID="unterminated\n',
    ],
)
def test_load_dotenv_rejects_malformed_assignments(
    tmp_path: Path,
    content: str,
) -> None:
    path = tmp_path / ".env"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=r"\.env"):
        config_module.load_dotenv(path)


def test_load_dotenv_supports_export_quotes_and_existing_precedence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        'export TEST_DOTENV_VALUE=" spaced value "\nTEST_DOTENV_EXISTING=from-file\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("TEST_DOTENV_VALUE", raising=False)
    monkeypatch.setenv("TEST_DOTENV_EXISTING", "from-process")

    config_module.load_dotenv(path)

    assert os.environ["TEST_DOTENV_VALUE"] == " spaced value "
    assert os.environ["TEST_DOTENV_EXISTING"] == "from-process"


def test_prefix_map_preserves_windows_drive_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "D:\\"
    target = "C:\\"
    monkeypatch.setenv("TEST_PREFIX_MAP", f"{source}={target}")

    assert config_module.env_string_prefix_map("TEST_PREFIX_MAP") == ((source, target),)


@pytest.mark.skipif(os.name != "nt", reason="Windows path comparison contract")
def test_prefix_map_duplicate_detection_matches_windows_translation_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PREFIX_MAP", "/Data=/first;/data=/second")

    with pytest.raises(ValueError, match="TEST_PREFIX_MAP"):
        config_module.env_string_prefix_map("TEST_PREFIX_MAP")


def test_blank_optional_storage_dir_remains_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv("ZOTERO_STORAGE_DIR", "   ")

    assert from_env(load_file=False).zotero_storage_dir is None


def test_env_path_rejects_overlong_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PATH", "x" * 4_097)

    with pytest.raises(ValueError, match="TEST_PATH"):
        config_module.env_path("TEST_PATH", Path("default"))


def test_zfr_library_bindings_are_count_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", json.dumps([{}] * 1_025))

    with pytest.raises(ValueError, match="at most 1024"):
        config_module.env_zfr_library_data_dirs()


def test_prefix_mappings_are_count_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mappings = ";".join(f"C:\\source{index}=C:\\target{index}" for index in range(129))
    monkeypatch.setenv("TEST_PREFIX_MAP", mappings)

    with pytest.raises(ValueError, match="at most 128"):
        config_module.env_string_prefix_map("TEST_PREFIX_MAP")


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("ZOTERO_INGEST_HOST", "bad host"),
        ("ZOTERO_INGEST_HOST", "host\nInjected"),
        ("METADATA_USER_AGENT", "agent\r\nInjected: yes"),
        ("SCIHUB_USER_AGENT", "agent\nInjected: yes"),
        ("ZOTERO_RELAY_TOKEN", "token\r\nInjected: yes"),
        ("ZOTERO_INGEST_TOKEN", "token with spaces"),
    ],
)
def test_from_env_rejects_ambiguous_host_or_header_values(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    raw: str,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv(name, raw)

    with pytest.raises(ValueError, match=name):
        from_env(load_file=False)


def test_from_env_accepts_exact_boundary_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv("ZOTERO_DISCOVERY_MAX_DEPTH", "32")
    monkeypatch.setenv("ZOTERO_INGEST_PORT", "65535")
    monkeypatch.setenv("METADATA_REQUEST_TIMEOUT_SECONDS", "86400")
    monkeypatch.setenv("METADATA_JOB_LEASE_SECONDS", "604800")
    monkeypatch.setenv("METADATA_DRAIN_MAX_WORKERS", "256")
    monkeypatch.setenv("ARXIV_SEARCH_MIN_SCORE", "1")
    monkeypatch.setenv("ZOTERO_SCAN_MAX_ITEMS", "1000000")

    config = from_env(load_file=False)

    assert config.zotero_discovery_max_depth == 32
    assert config.worker_port == 65_535
    assert config.metadata_request_timeout_seconds == 86_400
    assert config.metadata_job_lease_seconds == 604_800
    assert config.metadata_drain_max_workers == 256
    assert config.arxiv_search_min_score == 1.0
    assert config.scan_max_items == 1_000_000


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("TEST_BOOL", "\ntrue"),
        ("TEST_INT", "1\r"),
        ("TEST_FLOAT", "\t0.5"),
    ],
)
def test_scalar_env_rejects_control_wrapped_values(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    raw: str,
) -> None:
    monkeypatch.setenv(name, raw)

    with pytest.raises(ValueError, match=name):
        if name == "TEST_BOOL":
            env_bool(name)
        elif name == "TEST_INT":
            env_int(name, 0)
        else:
            env_float(name, 0.0)


@pytest.mark.parametrize("raw", ["\nC:\\data", "C:\\data\r", "C:\\da\tta"])
def test_env_path_rejects_control_characters(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv("TEST_PATH", raw)

    with pytest.raises(ValueError, match="TEST_PATH"):
        config_module.env_path("TEST_PATH", Path("default"))


def test_env_path_accepts_exact_character_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = "x" * 4_096
    monkeypatch.setenv("TEST_PATH", raw)

    assert str(config_module.env_path("TEST_PATH", Path("default"))) == raw


def test_env_path_list_rejects_control_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PATHS", "safe;bad\npath")

    with pytest.raises(ValueError, match=r"TEST_PATHS\[1\]"):
        config_module.env_path_list("TEST_PATHS")


def test_zfr_binding_paths_reject_control_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ZFR_LIBRARY_BINDINGS",
        json.dumps([{"hostDataDir": "C:\\data\nInjected"}]),
    )

    with pytest.raises(ValueError, match="ZFR_LIBRARY_BINDINGS"):
        config_module.env_zfr_library_data_dirs()


def test_prefix_map_paths_reject_control_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PREFIX_MAP", "C:\\data=C:\\target\nInjected")

    with pytest.raises(ValueError, match="TEST_PREFIX_MAP"):
        config_module.env_string_prefix_map("TEST_PREFIX_MAP")


def test_drive_root_prefix_translates_descendants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv("ZOTERO_PATH_PREFIX_MAP", f"D:\\={tmp_path}")

    config = from_env(load_file=False)

    assert (
        config.translate_zotero_input_path(r"d:\folder\paper.pdf")
        == tmp_path / "folder" / "paper.pdf"
    )


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("ZOTERO_RELAY_URL", "\r\nhttp://relay:23118"),
        ("ZOTERO_RELAY_URL", "http://relay:"),
    ],
)
def test_from_env_rejects_edge_controls_and_empty_url_port(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    raw: str,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv(name, raw)

    with pytest.raises(ValueError, match=name):
        from_env(load_file=False)


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("ZOTERO_INGEST_HOST", "\nhost"),
        ("METADATA_USER_AGENT", "agent\r"),
        ("SCIHUB_USER_AGENT", "\nagent"),
        ("ZOTERO_RELAY_TOKEN", "\rtoken"),
        ("ZOTERO_INGEST_TOKEN", "token\n"),
    ],
)
def test_from_env_rejects_edge_control_characters(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    raw: str,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv(name, raw)

    with pytest.raises(ValueError, match=name):
        from_env(load_file=False)


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("ZOTERO_INGEST_HOST", "h" * 254),
        ("METADATA_USER_AGENT", "a" * 1_025),
        ("ZOTERO_INGEST_TOKEN", "t" * 8_193),
    ],
)
def test_from_env_rejects_overlong_host_header_and_token(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    raw: str,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv(name, raw)

    with pytest.raises(ValueError, match=name):
        from_env(load_file=False)


def test_from_env_normalizes_valid_host_headers_and_token_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv("ZOTERO_INGEST_HOST", "  ::1  ")
    monkeypatch.setenv("ZOTERO_INGEST_TOKEN", "  abc.def-123_  ")
    monkeypatch.setenv("METADATA_USER_AGENT", "  test-agent/1.0 (contact)  ")
    monkeypatch.setenv("ZOTERO_RELAY_TOKEN", "   ")
    monkeypatch.setenv("ZFR_TOKEN", "relay-fallback")

    config = from_env(load_file=False)

    assert config.worker_host == "::1"
    assert config.worker_token == "abc.def-123_"
    assert config.metadata_user_agent == "test-agent/1.0 (contact)"
    assert config.zotero_relay_token == "relay-fallback"


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("METADATA_PATCH_POLICY", "\nemptyFieldsOnly"),
        ("ZOTERO_RELAY_REPLACE_STRATEGY", "replace\r"),
        ("ZOTERO_INGEST_ROLE", "\tall"),
    ],
)
def test_from_env_rejects_control_wrapped_choice_values(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    raw: str,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv(name, raw)

    with pytest.raises(ValueError, match=name):
        from_env(load_file=False)


def test_request_override_rejects_control_wrapped_strategy() -> None:
    config = from_env(load_file=False)

    with pytest.raises(ValueError, match=r"zotero\.relay_strategy"):
        apply_request_overrides(
            config,
            {"zotero": {"relay_strategy": "\nreplace"}},
        )


@pytest.mark.parametrize(
    ("kind", "raw"),
    [
        ("bool", "true\u2028"),
        ("path", "C:\\data\u200b"),
    ],
)
def test_config_values_reject_unicode_non_printable_characters(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    raw: str,
) -> None:
    name = "TEST_BOOL" if kind == "bool" else "TEST_PATH"
    monkeypatch.setenv(name, raw)

    with pytest.raises(ValueError, match=name):
        if kind == "bool":
            env_bool(name)
        else:
            config_module.env_path(name, Path("default"))


@pytest.mark.parametrize("raw", ["１２.５", "١.٥"])
def test_env_float_requires_ascii_numeric_syntax(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv("TEST_FLOAT", raw)

    with pytest.raises(ValueError, match="TEST_FLOAT"):
        env_float("TEST_FLOAT", 0.0)


def test_env_path_list_is_count_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exact = ";".join(f"path-{index}" for index in range(1_024))
    monkeypatch.setenv("TEST_PATHS", exact)
    assert len(config_module.env_path_list("TEST_PATHS")) == 1_024

    over = ";".join(f"path-{index}" for index in range(1_025))
    monkeypatch.setenv("TEST_PATHS", over)
    with pytest.raises(ValueError, match="at most 1024"):
        config_module.env_path_list("TEST_PATHS")


def test_prefix_map_rejects_edge_control_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PREFIX_MAP", "\nC:\\data=C:\\target")

    with pytest.raises(ValueError, match="TEST_PREFIX_MAP"):
        config_module.env_string_prefix_map("TEST_PREFIX_MAP")


def test_windows_prefix_matching_does_not_expand_casefolded_components(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv(
        "ZOTERO_PATH_PREFIX_MAP",
        rf"C:\Straße={tmp_path}",
    )

    config = from_env(load_file=False)
    raw = r"C:\STRASSE\paper.pdf"

    assert config.translate_zotero_input_path(raw) == Path(raw).expanduser()


@pytest.mark.parametrize(
    "raw",
    [
        "http://relay/path%1fvalue",
        "http://relay/path%7Fvalue",
        "http://relay/path%zz",
        "http://relay/path%",
    ],
)
def test_from_env_rejects_forbidden_or_malformed_url_escapes(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv("ZOTERO_RELAY_URL", raw)

    with pytest.raises(ValueError, match="ZOTERO_RELAY_URL"):
        from_env(load_file=False)


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("ZOTERO_INGEST_HOST", "хост"),
        ("METADATA_USER_AGENT", "агент/1.0"),
        ("SCIHUB_USER_AGENT", "агент/1.0"),
        ("ZOTERO_INGEST_TOKEN", "токен"),
        ("ZOTERO_RELAY_TOKEN", "токен"),
    ],
)
def test_from_env_rejects_non_ascii_transport_values(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    raw: str,
) -> None:
    monkeypatch.setenv("ZOTERO_AUTO_DISCOVER", "0")
    monkeypatch.setenv("ZFR_LIBRARY_BINDINGS", "[]")
    monkeypatch.setenv(name, raw)

    with pytest.raises(ValueError, match=name):
        from_env(load_file=False)


@pytest.mark.parametrize(
    "content",
    [
        b"BAD=\xff\n",
        b"BAD=value\x00suffix\n",
    ],
)
def test_load_dotenv_rejects_invalid_encoding_and_nul(
    tmp_path: Path,
    content: bytes,
) -> None:
    path = tmp_path / ".env"
    path.write_bytes(content)

    with pytest.raises(ValueError, match=r"\.env"):
        config_module.load_dotenv(path)


def test_load_dotenv_enforces_file_and_line_caps(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"#" + (b"x" * 1_000_000))
    with pytest.raises(ValueError, match="1000000"):
        config_module.load_dotenv(path)

    path.write_text("KEY=" + ("x" * 16_385) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line is too long"):
        config_module.load_dotenv(path)


def test_zfr_bindings_convert_deep_json_failure_to_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ZFR_LIBRARY_BINDINGS",
        ("[" * 1_100) + ("]" * 1_100),
    )

    with pytest.raises(ValueError, match="ZFR_LIBRARY_BINDINGS"):
        config_module.env_zfr_library_data_dirs()


def test_discovery_distinguishes_directories_when_inode_is_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_zotero_data_dir(first)
    _write_zotero_data_dir(second)
    original_stat = Path.stat

    def zero_directory_inode(
        path: Path,
        *,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        result = original_stat(path, follow_symlinks=follow_symlinks)
        if result.st_mode & 0o170000 != 0o040000:
            return result
        values = list(result)
        values[1] = 0
        return os.stat_result(values)

    monkeypatch.setattr(Path, "stat", zero_directory_inode)

    assert set(config_module.discover_zotero_data_dirs((tmp_path,), max_depth=1)) == {
        first,
        second,
    }
