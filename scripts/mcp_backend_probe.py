from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BackendProbe:
    ok: bool
    category: str
    phase: str
    retryable: bool
    endpoint: str
    message: str
    workspace: str | None = None
    tool_count: int | None = None
    tools: tuple[str, ...] = ()
    backend_state: str = "unavailable"
    cause_type: str | None = None
    cause_message: str | None = None
    recovery_hint: str | None = None
    permission_mode: str | None = None
    filesystem_scope: str | None = None
    dangerously_skip_all_permissions: bool | None = None
    dangerously_allow_any_local_path: bool | None = None
    annotation_override: str | None = None
    computer_use_enabled: bool | None = None


class BackendProbeError(RuntimeError):
    def __init__(
        self,
        category: str,
        phase: str,
        message: str,
        *,
        retryable: bool,
        backend_state: str = "unavailable",
        cause_type: str | None = None,
        cause_message: str | None = None,
        recovery_hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.phase = phase
        self.retryable = retryable
        self.backend_state = backend_state
        self.cause_type = cause_type
        self.cause_message = cause_message
        self.recovery_hint = recovery_hint


def tool_catalog_sha256(tools: tuple[str, ...] | list[str]) -> str:
    normalized = "\n".join(sorted({str(tool) for tool in tools if str(tool)}))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def load_authorization_header(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        value = path.expanduser().resolve(strict=True).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError("authorization header file could not be read") from exc
    if re.fullmatch(r"Bearer [A-Za-z0-9._~+/=-]{16,4096}", value) is None:
        raise ValueError("authorization header file must contain one Bearer credential")
    return value


def _parse_response(raw: bytes, content_type: str) -> dict[str, Any] | None:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    if "text/event-stream" in content_type or text.startswith(("event:", "data:")):
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if data and data != "[DONE]":
                payload = json.loads(data)
                return payload if isinstance(payload, dict) else None
        return None
    payload = json.loads(text)
    return payload if isinstance(payload, dict) else None


def _post(
    endpoint: str,
    payload: dict[str, Any],
    *,
    protocol_version: str,
    timeout: float,
    expect_reply: bool,
    authorization_header: str | None = None,
) -> dict[str, Any] | None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": protocol_version,
    }
    if authorization_header:
        headers["Authorization"] = authorization_header
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.getcode()
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        retryable = exc.code >= 500
        raise BackendProbeError(
            "authentication" if exc.code in {401, 403} else "upstream_http",
            "http",
            f"MCP endpoint returned HTTP {exc.code}",
            retryable=retryable,
            backend_state="rejected" if exc.code in {401, 403} else "upstream_error",
            cause_type="HTTPError",
            cause_message=f"HTTP {exc.code}",
            recovery_hint=(
                "Verify the configured credential and endpoint identity."
                if exc.code in {401, 403}
                else "Keep the tunnel unpublished until the local backend passes its MCP readiness probe."
            ),
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        underlying = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        cause_type = type(underlying).__name__
        if isinstance(underlying, (TimeoutError, socket.timeout)):
            cause_message = "connection timed out"
        elif isinstance(underlying, ConnectionRefusedError):
            cause_message = "connection refused"
        elif isinstance(underlying, OSError):
            code = getattr(underlying, "winerror", None) or getattr(underlying, "errno", None)
            cause_message = f"operating system error {code}" if code is not None else "operating system error"
        else:
            cause_message = "transport connection failed"
        raise BackendProbeError(
            "transport",
            "connect",
            f"MCP endpoint is unreachable: {cause_type}",
            retryable=True,
            cause_type=cause_type,
            cause_message=cause_message,
            recovery_hint="Start or repair the exact local MCP owner, then retry the readiness transaction.",
        ) from exc
    if not expect_reply and status in (200, 202, 204):
        return None
    try:
        parsed = _parse_response(raw, content_type)
    except (json.JSONDecodeError, ValueError) as exc:
        raise BackendProbeError(
            "protocol", "decode", "MCP endpoint returned invalid JSON", retryable=False
        ) from exc
    if parsed is None:
        raise BackendProbeError(
            "protocol", "decode", "MCP endpoint returned no JSON-RPC payload", retryable=False
        )
    return parsed


def _request(
    endpoint: str,
    request_id: int,
    method: str,
    params: dict[str, Any],
    *,
    protocol_version: str,
    timeout: float,
    authorization_header: str | None = None,
) -> dict[str, Any]:
    payload = _post(
        endpoint,
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        protocol_version=protocol_version,
        timeout=timeout,
        expect_reply=True,
        authorization_header=authorization_header,
    )
    assert payload is not None
    if payload.get("id") not in (request_id, None):
        raise BackendProbeError(
            "protocol", method, "MCP response id did not match the request", retryable=False
        )
    if "error" in payload:
        raise BackendProbeError(
            "protocol", method, f"MCP {method} returned a JSON-RPC error", retryable=False
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise BackendProbeError(
            "protocol", method, f"MCP {method} result was not an object", retryable=False
        )
    return result


def _normalized_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(str(Path(value).expanduser().resolve())))


def probe_mcp_backend(
    endpoint: str,
    *,
    expected_workspace: str,
    required_tools: tuple[str, ...],
    timeout: float,
    authorization_header: str | None = None,
) -> BackendProbe:
    protocol_version = "2025-11-25"
    try:
        initialized = _request(
            endpoint,
            1,
            "initialize",
            {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "coding-tools-mcp-watchdog", "version": "0.1"},
            },
            protocol_version=protocol_version,
            timeout=timeout,
            authorization_header=authorization_header,
        )
        negotiated = initialized.get("protocolVersion")
        if isinstance(negotiated, str) and negotiated:
            protocol_version = negotiated
        _post(
            endpoint,
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            protocol_version=protocol_version,
            timeout=timeout,
            expect_reply=False,
            authorization_header=authorization_header,
        )
        listed = _request(
            endpoint,
            2,
            "tools/list",
            {},
            protocol_version=protocol_version,
            timeout=timeout,
            authorization_header=authorization_header,
        )
        raw_tools = listed.get("tools")
        if not isinstance(raw_tools, list):
            raise BackendProbeError(
                "catalog",
                "tools/list",
                "MCP tools/list did not return a tools array",
                retryable=False,
                backend_state="catalog_invalid",
                recovery_hint="Repair the server catalog before publishing the tunnel.",
            )
        tool_names = tuple(
            sorted(tool["name"] for tool in raw_tools if isinstance(tool, dict) and isinstance(tool.get("name"), str))
        )
        missing = tuple(sorted(set(required_tools) - set(tool_names)))
        if missing:
            raise BackendProbeError(
                "catalog",
                "tools/list",
                "MCP tool catalog is missing required tools: " + ", ".join(missing),
                retryable=False,
                backend_state="catalog_mismatch",
                recovery_hint="Refresh the server deployment and connector schema from the same catalog version.",
            )
        called = _request(
            endpoint,
            3,
            "tools/call",
            {"name": "server_info", "arguments": {}},
            protocol_version=protocol_version,
            timeout=timeout,
            authorization_header=authorization_header,
        )
        structured = called.get("structuredContent")
        if not isinstance(structured, dict):
            raise BackendProbeError(
                "identity", "server_info", "server_info returned no structured content", retryable=False
            )
        workspace = structured.get("workspace")
        if not isinstance(workspace, str) or _normalized_path(workspace) != _normalized_path(expected_workspace):
            raise BackendProbeError(
                "identity",
                "server_info",
                "MCP backend workspace does not match the configured control plane",
                retryable=False,
                backend_state="wrong_workspace",
                recovery_hint="Keep the existing process isolated and start the expected workspace on its assigned port.",
            )
        return BackendProbe(
            True,
            "ready",
            "server_info",
            False,
            endpoint,
            "MCP backend is ready",
            workspace=workspace,
            tool_count=len(tool_names),
            tools=tool_names,
            backend_state="ready",
            permission_mode=(
                structured.get("permission_mode")
                if isinstance(structured.get("permission_mode"), str)
                else None
            ),
            filesystem_scope=(
                structured.get("filesystem_scope")
                if isinstance(structured.get("filesystem_scope"), str)
                else None
            ),
            dangerously_skip_all_permissions=(
                structured.get("dangerously_skip_all_permissions")
                if isinstance(structured.get("dangerously_skip_all_permissions"), bool)
                else None
            ),
            dangerously_allow_any_local_path=(
                structured.get("dangerously_allow_any_local_path")
                if isinstance(structured.get("dangerously_allow_any_local_path"), bool)
                else None
            ),
            annotation_override=(
                structured.get("annotation_override")
                if isinstance(structured.get("annotation_override"), str)
                else None
            ),
            computer_use_enabled=(
                structured.get("computer_use", {}).get("enabled")
                if isinstance(structured.get("computer_use"), dict)
                and isinstance(structured["computer_use"].get("enabled"), bool)
                else None
            ),
        )
    except BackendProbeError as exc:
        return BackendProbe(
            False,
            exc.category,
            exc.phase,
            exc.retryable,
            endpoint,
            str(exc),
            backend_state=exc.backend_state,
            cause_type=exc.cause_type,
            cause_message=exc.cause_message,
            recovery_hint=exc.recovery_hint,
        )
