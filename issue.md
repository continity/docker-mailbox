# MCP endpoint `/mcp` 307-redirects to `/mcp/`, which Claude Desktop cannot use

## Summary

`POST /mcp` (no trailing slash) is not served. Starlette’s `Mount("/mcp")` does not match that path and FastAPI/Starlette answers with **307 Temporary Redirect** to `/mcp/`.

That is unusable from **Claude Desktop**. The app **strips a trailing slash** from the server URL, so the configured endpoint is always `/mcp`, never `/mcp/`. The client does not follow the 307 on the streamable-HTTP POST, so the MCP connection never comes up.

REST routes in the same process do not behave this way: `GET /mailboxes` works without a trailing slash.

## Why this matters

- Claude Desktop users cannot enter a working URL. `/mcp/` is rewritten to `/mcp`, which redirects and fails.
- Most MCP clients (and `streamablehttp_client`) do **not** follow 307 on POST, or do not preserve the body/headers correctly.
- A trailing slash is not required on the other public routes (`/health`, `/mailboxes`, `/inbox`). Clients reasonably expect `/mcp` to work the same way.

## Reproduction

```bash
# REST: no trailing slash required
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" http://localhost:8000/mailboxes
# 200

# MCP without slash: redirect
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" \
  -X POST http://localhost:8000/mcp \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
# 307  http://localhost:8000/mcp/

# MCP with slash: handler runs
curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST http://localhost:8000/mcp/ \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
# 200
```

Docs and `server.json` tell clients to use `http://localhost:8000/mcp` (no slash). That is exactly the URL that 307s.

## Expected

`/mcp` and `/mcp/` should both reach the streamable-HTTP handler with **no redirect**, same as `/mailboxes`.

## Actual

| Path | Result |
|---|---|
| `GET /mailboxes` | 200 |
| `POST /mcp` | **307 → `/mcp/`** |
| `POST /mcp/` | 200 (MCP handler) |

## Cause

The MCP transport is mounted as a Starlette `Mount("/mcp", ...)`, which compiles to `/mcp/{path:path}` and only matches paths **with** a slash after `mcp`. FastAPI’s default `redirect_slashes=True` then 307s `/mcp` → `/mcp/`.

Existing tests post to `"/mcp"` via `TestClient`, which **follows redirects**, so they never see the 307.

## Suggested fix

Keep `Mount("/mcp", ...)` and rewrite `path == "/mcp"` to `"/mcp/"` in a small pure-ASGI wrapper **before** the router, in the same style as the existing `_BearerASGI` wrapper. After that rewrite the Mount matches, the remaining path is `/`, and the MCP handler runs with no 307.

```python
class _MCPSlashNormalizer:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            scope = dict(scope)
            scope["path"] = "/mcp/"
        await self._app(scope, receive, send)
```

Register it **inside** auth so `_BearerASGI` still sees the path the client sent:

```python
app.router.routes.append(Mount("/mcp", app=mcp_manager.handle_request))
app.add_middleware(_MCPSlashNormalizer)  # innermost, next to the router
app.add_middleware(_BearerASGI, cfg=cfg)  # outermost
```

What not to do:

- Do **not** set `redirect_slashes=False` on the whole app — that changes `/mailboxes/` and every other route.
- Do **not** use `Mount("/mcp/")` — Starlette rejects a trailing slash on the Mount path.
- Do **not** add a FastAPI `@app.post("/mcp")` next to the Mount — `Route` expects `request → response`, and the MCP handler is raw ASGI (streaming).

Cover it with a `TestClient(..., follow_redirects=False)` test. The existing MCP tests post to `"/mcp"` with redirects **on**, so they stay green even while the 307 is still there.

```python
def test_mcp_post_without_trailing_slash_is_not_redirected(tmp_path):
    with TestClient(app, follow_redirects=False) as client:
        response = client.post("/mcp", headers=..., json=initialize_body)
    assert response.status_code != 307
    assert response.status_code == 200
```

`/mcp/` should keep working the same way (no redirect, 200).
