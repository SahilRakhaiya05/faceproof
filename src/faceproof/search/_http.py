from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .base import SearchError


@dataclass(frozen=True, slots=True)
class BoundedJsonResponse:
    body: dict[str, Any]
    raw: bytes
    status_code: int
    headers: httpx.Headers


def request_json_object(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    operation: str,
    max_bytes: int,
    params: Any = None,
    data: Any = None,
    files: Any = None,
) -> BoundedJsonResponse:
    """Fetch strict JSON without transparent decompression or unbounded buffering."""

    try:
        with client.stream(
            method,
            url,
            params=params,
            data=data,
            files=files,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                raise SearchError(f"{operation} failed with HTTP {response.status_code}")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].casefold()
            if content_type != "application/json" and not content_type.endswith("+json"):
                raise SearchError(f"{operation} returned an unexpected content type")
            content_encoding = response.headers.get("content-encoding", "").strip().casefold()
            if content_encoding not in {"", "identity"}:
                raise SearchError(f"{operation} returned an encoded response body")
            declared_length = response.headers.get("content-length")
            if declared_length:
                try:
                    parsed_length = int(declared_length)
                except ValueError as exc:
                    raise SearchError(f"{operation} returned an invalid Content-Length") from exc
                if parsed_length < 0 or parsed_length > max_bytes:
                    raise SearchError(f"{operation} returned an oversized response")
            if response.is_stream_consumed:
                raw = response.content
                if len(raw) > max_bytes:
                    raise SearchError(f"{operation} returned an oversized response")
            else:
                timeout_value = client.timeout.read
                total_budget = (
                    float(timeout_value)
                    if (
                        isinstance(timeout_value, (int, float))
                        and timeout_value > 0
                        and math.isfinite(timeout_value)
                    )
                    else 30.0
                )
                deadline = time.monotonic() + total_budget
                output = bytearray()
                # Yield raw transport chunks so a peer cannot evade the
                # cumulative deadline by dripping sub-buffer-sized pieces.
                for chunk in response.iter_raw(chunk_size=None):
                    if time.monotonic() > deadline:
                        raise SearchError(f"{operation} exceeded the total read deadline")
                    if len(chunk) > max_bytes - len(output):
                        raise SearchError(f"{operation} returned an oversized response")
                    output.extend(chunk)
                raw = bytes(output)
            status_code = response.status_code
            headers = httpx.Headers(response.headers)
    except SearchError:
        raise
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError, ValueError) as exc:
        raise SearchError(f"{operation} failed ({type(exc).__name__})") from exc

    if not raw:
        raise SearchError(f"{operation} returned an empty response")
    try:
        body = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
            parse_float=_finite_float,
        )
        _validate_json_tree(body)
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SearchError(f"{operation} returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise SearchError(f"{operation} returned a non-object response")
    return BoundedJsonResponse(
        body=body,
        raw=raw,
        status_code=status_code,
        headers=headers,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _validate_json_tree(value: Any, *, max_depth: int = 64, max_nodes: int = 100_000) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            raise ValueError("JSON document has too many values")
        if depth > max_depth:
            raise ValueError("JSON document is nested too deeply")
        if isinstance(current, dict):
            for key, item in current.items():
                _reject_surrogates(key)
                stack.append((item, depth + 1))
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str):
            _reject_surrogates(current)
        elif isinstance(current, float) and not math.isfinite(current):
            raise ValueError("JSON document contains a non-finite number")


def _reject_surrogates(value: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError("JSON document contains a lone Unicode surrogate")
