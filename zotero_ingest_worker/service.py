from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .config import WorkerConfig, from_env
from .full_run import FullRunManager
from .metadata_jobs import METADATA_JOB_ENRICH, METADATA_JOB_FULL_TEXT
from .metadata_processor import ZoteroMetadataProcessor
from .service_actions import run_post_action
from .worker_roles import (
    ROLE_FULLTEXT,
    ROLE_METADATA,
    normalize_worker_role,
    post_action_paths_for_role,
    role_mode_label,
)


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


def _build_handler(base_config: WorkerConfig, full_run_manager: FullRunManager, *, role: str):
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
                        "zotero_data_dirs": [str(path) for path in base_config.zotero_data_dirs],
                        "state_db": str(base_config.state_db_path),
                    },
                )
                return
            if parsed.path == "/api/zotero/metadata/queue":
                if not self._authorized():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Unauthorized."})
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
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Unauthorized."})
                    return
                self._send_json(HTTPStatus.OK, full_run_manager.status())
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found."})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path not in post_action_paths:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Not found."})
                return
            if not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Unauthorized."})
                return

            try:
                payload = self._read_json_body()
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
            except Exception as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

        def _authorized(self) -> bool:
            if not base_config.worker_token:
                return True
            header_token = self.headers.get("X-Zotero-Worker-Token", "")
            auth = self.headers.get("Authorization", "")
            bearer = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
            return header_token == base_config.worker_token or bearer == base_config.worker_token

        def _read_json_body(self) -> dict[str, Any]:
            raw_len = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_len)
            except ValueError as exc:
                raise ValueError("Invalid Content-Length header.") from exc
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw) if raw.strip() else {}

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
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
