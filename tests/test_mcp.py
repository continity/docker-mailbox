"""Streamable-HTTP MCP contract tests through the mounted production route."""

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from httpx import Response

from mailboxd.config import load_config
from mailboxd.server import create_app

_MCP_PATH = "/mcp"
_JSONRPC_VERSION = "2.0"
_SSE_DATA_PREFIX = "data: "
_TEST_TOKEN = "test-token"
_HEADERS = {
    "Authorization": f"Bearer {_TEST_TOKEN}",
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _write_cfg(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"""
auth:
  tokens: [{_TEST_TOKEN}]
mailboxes:
  - name: alpha
    description: Primary mailbox
    imap: {{host: imap.example.com, username: alpha@example.com, password: p}}
    smtp: {{host: smtp.example.com, username: alpha@example.com, password: p,
      from_address: alpha@example.com}}
""")
    return config_path


def _client(tmp_path: Path, *, follow_redirects: bool = True) -> TestClient:
    return TestClient(
        create_app(load_config(str(_write_cfg(tmp_path)))),
        follow_redirects=follow_redirects,
    )


def _mcp_payload(response: Response) -> dict[str, Any]:
    assert response.status_code == 200
    if response.headers["content-type"].startswith("application/json"):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith(_SSE_DATA_PREFIX):
            return json.loads(line.removeprefix(_SSE_DATA_PREFIX))
    raise AssertionError(f"MCP response had no JSON payload: {response.text!r}")


def _mcp_request(
    client: TestClient,
    request_id: int,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    response = client.post(
        _MCP_PATH,
        headers=_HEADERS,
        json={
            "jsonrpc": _JSONRPC_VERSION,
            "id": request_id,
            "method": method,
            "params": params,
        },
    )
    return _mcp_payload(response)


def _initialize_body() -> dict[str, Any]:
    return {
        "jsonrpc": _JSONRPC_VERSION,
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "mailboxd-test", "version": "1"},
        },
    }


def test_mcp_post_without_trailing_slash_is_not_redirected(tmp_path: Path) -> None:
    """Claude Desktop POSTs /mcp and does not follow Starlette's 307 to /mcp/."""
    with _client(tmp_path, follow_redirects=False) as client:
        response = client.post("/mcp", headers=_HEADERS, json=_initialize_body())
    assert response.status_code != 307
    assert response.status_code == 200


def test_mcp_post_with_trailing_slash_still_works(tmp_path: Path) -> None:
    with _client(tmp_path, follow_redirects=False) as client:
        response = client.post("/mcp/", headers=_HEADERS, json=_initialize_body())
    assert response.status_code != 307
    assert response.status_code == 200


def test_streamable_http_mcp_initializes_lists_tools_and_calls_mailboxes(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        initialized = _mcp_request(
            client,
            1,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "mailboxd-test", "version": "1"},
            },
        )
        tools = _mcp_request(client, 2, "tools/list", {})
        mailbox_result = _mcp_request(
            client,
            3,
            "tools/call",
            {"name": "mailboxes", "arguments": {}},
        )

    assert initialized["id"] == 1
    assert initialized["result"]["serverInfo"]["name"] == "mailboxd"
    tool_names = {tool["name"] for tool in tools["result"]["tools"]}
    assert {"mailboxes", "inbox", "list_folders", "send"} <= tool_names
    content = mailbox_result["result"]["content"]
    assert json.loads(content[0]["text"]) == {
        "mailboxes": [
            {
                "name": "alpha",
                "description": "Primary mailbox",
                "address": "alpha@example.com",
                "imap": True,
                "smtp": True,
            }
        ]
    }
