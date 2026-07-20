from __future__ import annotations

from email.message import Message
from http import HTTPStatus
from io import BytesIO
from typing import Any

import pytest

from zotero_ingest_worker.config import from_env
from zotero_ingest_worker.service import (
    MAX_REQUEST_BODY_BYTES,
    MAX_REQUEST_JSON_DEPTH,
    MAX_REQUEST_JSON_VALUES,
    _build_handler,
    _read_json_request_body,
)


def _read(raw: bytes) -> dict[str, Any]:
    return _read_json_request_body(
        BytesIO(raw),
        content_lengths=[str(len(raw))],
    )


def test_json_request_body_accepts_empty_and_object_payloads() -> None:
    assert _read_json_request_body(BytesIO(), content_lengths=None) == {}
    assert _read(b"  \r\n") == {}
    assert _read(b'{"limit": 7}') == {"limit": 7}


@pytest.mark.parametrize(
    "content_lengths",
    [
        [],
        [""],
        ["-1"],
        ["+1"],
        [" 1"],
        ["1 "],
        ["1", "1"],
        [str(MAX_REQUEST_BODY_BYTES + 1)],
    ],
)
def test_json_request_body_rejects_ambiguous_or_unbounded_length(
    content_lengths: list[str],
) -> None:
    with pytest.raises(ValueError, match="Content-Length|body"):
        _read_json_request_body(BytesIO(b"{}"), content_lengths=content_lengths)


def test_json_request_body_rejects_transfer_encoding() -> None:
    with pytest.raises(ValueError, match="Transfer-Encoding"):
        _read_json_request_body(
            BytesIO(b"{}"),
            content_lengths=["2"],
            transfer_encoding="chunked",
        )


def test_json_request_body_rejects_short_read() -> None:
    with pytest.raises(ValueError, match="shorter"):
        _read_json_request_body(BytesIO(b"{}"), content_lengths=["3"])


@pytest.mark.parametrize(
    "raw, message",
    [
        (b"\xff", "UTF-8"),
        (b"{", "JSON"),
        (b"null", "object"),
        (b"[]", "object"),
        (b"true", "object"),
        (b"1", "object"),
        (b'{"value": NaN}', "finite"),
        (b'{"value": Infinity}', "finite"),
        (b'{"limit": 1, "limit": 2}', "duplicate"),
    ],
)
def test_json_request_body_rejects_malformed_or_ambiguous_json(
    raw: bytes,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _read(raw)


def _post_response(
    raw: bytes,
    *,
    manager: object,
    content_types: list[str] | None = None,
) -> tuple[int, dict[str, Any]]:
    handler_type = _build_handler(
        from_env(load_file=False),
        manager,  # type: ignore[arg-type]
        role="all",
    )
    handler = object.__new__(handler_type)
    handler.path = "/api/zotero/pipeline/full-run/status"
    headers = Message()
    headers["Content-Length"] = str(len(raw))
    for content_type in content_types or []:
        headers["Content-Type"] = content_type
    handler.headers = headers
    handler.rfile = BytesIO(raw)
    responses: list[tuple[int, dict[str, Any]]] = []
    handler._authorized = lambda: True  # type: ignore[method-assign]
    handler._send_json = (  # type: ignore[method-assign]
        lambda status, payload: responses.append((status, payload))
    )

    handler.do_POST()

    assert len(responses) == 1
    return responses[0]


def test_http_post_maps_malformed_client_payload_to_400() -> None:
    class UnexpectedManager:
        def status(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("Malformed JSON must not reach the action.")

    status, payload = _post_response(b"[]", manager=UnexpectedManager())

    assert status == HTTPStatus.BAD_REQUEST
    assert payload["ok"] is False
    assert "object" in payload["error"]


def test_http_post_hides_unexpected_internal_error_details() -> None:
    class FailingManager:
        def status(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("database password leaked here")

    status, payload = _post_response(b"{}", manager=FailingManager())

    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert payload == {"ok": False, "error": "Internal server error."}


@pytest.mark.parametrize(
    "raw, message",
    [
        (b'{"value": 1e999}', "finite"),
        (b'{"value": "\\u0000"}', "printable"),
        (b'{"value": "\\ud800"}', "printable"),
        (b'{"bad\\nkey": 1}', "printable"),
    ],
)
def test_json_request_body_rejects_nonfinite_or_nonprintable_decoded_values(
    raw: bytes,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _read(raw)


def test_json_request_body_rejects_excessive_nesting() -> None:
    value: object = 0
    for _ in range(MAX_REQUEST_JSON_DEPTH):
        value = [value]
    raw = ('{"value":' + str(value).replace(" ", "") + "}").encode("ascii")

    with pytest.raises(ValueError, match="nesting"):
        _read(raw)


def test_json_request_body_rejects_excessive_value_count() -> None:
    raw = (
        '{"values":[' + ",".join("0" for _ in range(MAX_REQUEST_JSON_VALUES)) + "]}"
    ).encode("ascii")
    assert len(raw) < MAX_REQUEST_BODY_BYTES

    with pytest.raises(ValueError, match="too many values"):
        _read(raw)


def test_json_request_body_rejects_unreasonably_long_length_integer() -> None:
    with pytest.raises(ValueError, match="Content-Length"):
        _read_json_request_body(
            BytesIO(),
            content_lengths=["9" * 10_000],
        )


@pytest.mark.parametrize(
    "content_types",
    [
        ["text/plain"],
        ["application/json", "application/json"],
        ["application/json; charset=utf-16"],
        ["application/json\nX-Injected: yes"],
    ],
)
def test_http_post_rejects_unsupported_or_ambiguous_content_type(
    content_types: list[str],
) -> None:
    class UnexpectedManager:
        def status(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("Invalid content type must not reach the action.")

    status, payload = _post_response(
        b"{}",
        manager=UnexpectedManager(),
        content_types=content_types,
    )

    assert status == HTTPStatus.BAD_REQUEST
    assert payload["ok"] is False
    assert "Content-Type" in payload["error"]


@pytest.mark.parametrize(
    "content_types",
    [
        None,
        ["application/json"],
        ["application/json; charset=UTF-8"],
        ["application/problem+json"],
    ],
)
def test_http_post_accepts_compatible_json_content_type(
    content_types: list[str] | None,
) -> None:
    class SuccessfulManager:
        def status(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"ok": True}

    status, payload = _post_response(
        b"{}",
        manager=SuccessfulManager(),
        content_types=content_types,
    )

    assert status == HTTPStatus.OK
    assert payload == {"ok": True}


def test_http_post_maps_action_validation_error_to_400() -> None:
    class UnexpectedManager:
        def status(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("Invalid action input must fail during preflight.")

    status, payload = _post_response(
        b'{"event_limit":1001}',
        manager=UnexpectedManager(),
        content_types=["application/json"],
    )

    assert status == HTTPStatus.BAD_REQUEST
    assert payload["ok"] is False
    assert "event_limit" in payload["error"]


def test_http_post_does_not_misclassify_internal_value_error_as_client_error() -> None:
    class FailingManager:
        def status(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise ValueError("internal state invariant failed")

    status, payload = _post_response(
        b"{}",
        manager=FailingManager(),
        content_types=["application/json"],
    )

    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert payload == {"ok": False, "error": "Internal server error."}
