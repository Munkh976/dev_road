"""
Datasette plugin: refuse any request whose Host header is not local.

Why: DNS rebinding. A hostile page can point its own domain at 127.0.0.1, and
the browser then treats requests to localhost:8001 as same-origin for that
domain. The Origin check in approval.py would pass (Origin and Host both say
"evil.example"), and the page could READ proposals, signals and the journal.
Browsers cannot forge the Host header, so an allowlist closes it.

This is an ASGI wrapper, not a route guard, so it covers every path including
Datasette's own table and query pages, and every method including GET.
Fails closed: a missing or unparseable Host is refused.
"""

from __future__ import annotations

from datasette import hookimpl

ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})


def host_without_port(raw: str) -> str:
    """'localhost:8001' -> 'localhost'; '[::1]:8001' -> '[::1]'."""
    raw = raw.strip().lower()
    if raw.startswith("["):
        end = raw.find("]")
        return raw[: end + 1] if end != -1 else raw
    return raw.rsplit(":", 1)[0] if ":" in raw else raw


def is_local_host(raw: str | None) -> bool:
    return raw is not None and host_without_port(raw) in ALLOWED_HOSTS


@hookimpl
def asgi_wrapper(datasette):
    def wrap(app):
        async def guarded(scope, receive, send):
            # "lifespan" has no Host header and must pass through untouched.
            if scope["type"] in {"http", "websocket"}:
                headers = {k.lower(): v for k, v in scope.get("headers", [])}
                host = headers.get(b"host")
                if not is_local_host(host.decode("latin-1") if host else None):
                    if scope["type"] == "websocket":
                        await send({"type": "websocket.close"})
                        return
                    body = b"Forbidden: non-local Host header"
                    await send({
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [
                            (b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    })
                    await send({"type": "http.response.body", "body": body})
                    return
            await app(scope, receive, send)

        return guarded

    return wrap
