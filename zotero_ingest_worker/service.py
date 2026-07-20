from __future__ import annotations

import json
import logging
import math
from email.message import Message
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BufferedIOBase
from typing import Any
from urllib.parse import urlparse

from .config import WorkerConfig, from_env
from .full_run import FullRunManager
from .metadata_jobs import METADATA_JOB_ENRICH, METADATA_JOB_FULL_TEXT
from .metadata_processor import ZoteroMetadataProcessor
from .service_actions import ActionRequestError, run_post_action
from .worker_roles import (
    ROLE_FULLTEXT,
    ROLE_METADATA,
    normalize_worker_role,
    post_action_paths_for_role,
    role_mode_label,
)


MAX_REQUEST_BODY_BYTES = 8_000_000
MAX_REQUEST_CONTENT_TYPE_CHARS = 256
MAX_REQUEST_JSON_DEPTH = 16
MAX_REQUEST_JSON_VALUES = 200_000
_LOGGER = logging.getLogger(__name__)


def _validate_json_content_type(content_types: list[str] | None) -> None:
    if not content_types:
        return
    if len(content_types) != 1:
        raise ValueError("Request must contain exactly one Content-Type header.")
    raw = content_types[0]
    if (
        not raw
        or len(raw) > MAX_REQUEST_CONTENT_TYPE_CHARS
        or any(not character.isprintable() for character in raw)
    ):
        raise ValueError("Invalid Content-Type header.")

    header = Message()
    header["Content-Type"] = raw
    media_type = header.get_content_type().lower()
    if not (
        media_type == "application/json"
        or (media_type.startswith("application/") and media_type.endswith("+json"))
    ):
        raise ValueError("Content-Type must identify a JSON media type.")

    parameters = header.get_params(header="Content-Type", failobj=[]) or []
    charsets = [
        value for name, value in parameters[1:] if str(name).lower() == "charset"
    ]
    if len(charsets) > 1:
        raise ValueError("Content-Type must contain at most one charset parameter.")
    if charsets:
        charset = charsets[0]
        if not isinstance(charset, str) or charset.strip().lower().replace(
            "_", "-"
        ) not in {"utf-8", "utf8"}:
            raise ValueError("Content-Type charset must be UTF-8.")


def _read_json_request_body(
    stream: BufferedIOBase,
    *,
    content_lengths: list[str] | None,
    transfer_encoding: str | None = None,
) -> dict[str, Any]:
    if transfer_encoding is not None and transfer_encoding.strip():
        raise ValueError("Transfer-Encoding is not supported.")
    if content_lengths is None:
        length = 0
    else:
        if len(content_lengths) != 1:
            raise ValueError("Request must contain exactly one Content-Length header.")
        raw_length = content_lengths[0]
        if not raw_length or not raw_length.isascii() or not raw_length.isdecimal():
            raise ValueError("Invalid Content-Length header.")
        if len(raw_length) > 20:
            raise ValueError("Invalid Content-Length header.")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length header.") from exc
    if length > MAX_REQUEST_BODY_BYTES:
        raise ValueError(f"Request body exceeds {MAX_REQUEST_BODY_BYTES} bytes.")
    if length == 0:
        return {}

    raw = stream.read(length)
    if len(raw) != length:
        raise ValueError("Request body is shorter than Content-Length.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Request body must be valid UTF-8.") from exc
    if not text.strip():
        return {}
    try:
        payload = json.loads(
            text,
            parse_constant=_reject_nonfinite_json_number,
            object_pairs_hook=_json_object_without_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        raise ValueError("Request body must contain valid JSON.") from exc
    except RecursionError as exc:
        raise ValueError("Request JSON nesting is too deep.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Request JSON must be an object.")
    _validate_request_json_complexity(payload)
    return payload


def _reject_nonfinite_json_number(_value: str) -> None:
    raise ValueError("JSON numbers must be finite.")


def _json_object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Request JSON contains a duplicate object key.")
        result[key] = value
    return result


def _validate_request_json_complexity(payload: dict[str, Any]) -> None:
    pending: list[tuple[object, int]] = [(payload, 1)]
    values = 0
    while pending:
        value, depth = pending.pop()
        values += 1
        if values > MAX_REQUEST_JSON_VALUES:
            raise ValueError("Request JSON contains too many values.")
        if depth > MAX_REQUEST_JSON_DEPTH:
            raise ValueError("Request JSON nesting is too deep.")
        if isinstance(value, dict):
            for key in value:
                if any(not character.isprintable() for character in key):
                    raise ValueError(
                        "Request JSON strings must contain only printable characters."
                    )
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
        elif isinstance(value, str):
            if any(not character.isprintable() for character in value):
                raise ValueError(
                    "Request JSON strings must contain only printable characters."
                )
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("JSON numbers must be finite.")


def run_server(config: WorkerConfig | None = None, *, role: str | None = None) -> None:
    cfg = config or from_env()
    server_role = normalize_worker_role(role or cfg.worker_role)
    full_run_manager = FullRunManager(cfg)
    handler = _build_handler(cfg, full_run_manager, role=server_role)
    server = ThreadingHTTPServer((cfg.worker_host, cfg.worker_port), handler)
    print(
        (
            f"zotero-ingest-worker listening on http://{cfg.worker_host}:{cfg.worker_port} "
            f"as {role_mode_label(server_role)}"
        ),
        flush=True,
    )
    print(
        (
            "worker endpoints: "
            + ", ".join(sorted(post_action_paths_for_role(server_role))[:6])
            + (" ..." if len(post_action_paths_for_role(server_role)) > 6 else "")
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _build_handler(
    base_config: WorkerConfig,
    full_run_manager: FullRunManager,
    *,
    role: str,
) -> type[BaseHTTPRequestHandler]:
    post_action_paths = post_action_paths_for_role(role)

    class ZoteroIngestHandler(BaseHTTPRequestHandler):
        server_version = "ZoteroIngestHTTP/0.1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "service": "zotero-ingest-worker",
                        "mode": role_mode_label(role),
                        "role": role,
                        "post_action_paths": sorted(post_action_paths),
                        "zotero_discovery_roots": [
                            str(path) for path in base_config.zotero_discovery_roots
                        ],
                        "zotero_data_dirs": [
                            str(path) for path in base_config.zotero_data_dirs
                        ],
                        "state_db": str(base_config.state_db_path),
                    },
                )
                return
            if parsed.path == "/api/zotero/metadata/queue":
                if not self._authorized():
                    self._send_json(
                        HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Unauthorized."}
                    )
                    return
                processor = ZoteroMetadataProcessor(base_config)
                self._send_json(
                    HTTPStatus.OK,
                    processor.queue(
                        job_type=_default_queue_job_type(role),
                        statuses=None,
                        limit=100,
                    ),
                )
                return
            if parsed.path == "/api/zotero/pipeline/full-run/status":
                if not self._authorized():
                    self._send_json(
                        HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Unauthorized."}
                    )
                    return
                self._send_json(HTTPStatus.OK, full_run_manager.status())
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found."})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path not in post_action_paths:
                self._send_json(
                    HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found."}
                )
                return
            if not self._authorized():
                self._send_json(
                    HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Unauthorized."}
                )
                return

            try:
                payload = self._read_json_body()
            except ValueError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": str(exc)},
                )
                return
            except Exception:
                _LOGGER.exception(
                    "Unhandled ingest request read failure: %s", parsed.path
                )
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"ok": False, "error": "Internal server error."},
                )
                return

            try:
                result = run_post_action(
                    parsed.path,
                    base_config,
                    payload,
                    full_run_manager,
                    role=role,
                )
                self._send_json(HTTPStatus.OK, result)
            except PermissionError as exc:
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": str(exc)})
            except ActionRequestError as exc:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error": str(exc)},
                )
            except Exception:
                _LOGGER.exception(
                    "Unhandled ingest service POST failure: %s", parsed.path
                )
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"ok": False, "error": "Internal server error."},
                )

        def _authorized(self) -> bool:
            if not base_config.worker_token:
                return True
            header_token = self.headers.get("X-Zotero-Worker-Token", "")
            auth = self.headers.get("Authorization", "")
            bearer = (
                auth.removeprefix("Bearer ").strip()
                if auth.startswith("Bearer ")
                else ""
            )
            return (
                header_token == base_config.worker_token
                or bearer == base_config.worker_token
            )

        def _read_json_body(self) -> dict[str, Any]:
            _validate_json_content_type(self.headers.get_all("Content-Type"))
            return _read_json_request_body(
                self.rfile,
                content_lengths=self.headers.get_all("Content-Length"),
                transfer_encoding=self.headers.get("Transfer-Encoding"),
            )

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return ZoteroIngestHandler


def _default_queue_job_type(role: str) -> str | None:
    if role == ROLE_METADATA:
        return METADATA_JOB_ENRICH
    if role == ROLE_FULLTEXT:
        return METADATA_JOB_FULL_TEXT
    return None
