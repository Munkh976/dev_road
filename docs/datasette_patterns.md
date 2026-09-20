# Datasette plugin patterns

Three patterns, extracted from a prior project and checked against Datasette
0.65.5 (the installed version). Single user on localhost: no auth, sessions,
roles or deployment config belongs here.

Run command (from `CLAUDE.md`):

```
datasette serve db\operations.db --metadata datasette\metadata.json ^
  --plugins-dir datasette\plugins --template-dir datasette\templates --reload
```

---

## 1. `register_routes()`

A route is `(regex, handler)`. Datasette calls the handler with only the
arguments it names, so `(request, datasette)` order does not matter.

```python
@hookimpl
def register_routes():
    return [(r"^/-/approve$", approve_proposal)]

async def approve_proposal(request, datasette): ...
```

- Anchor every regex with `^...$`; unanchored patterns match as substrings.
- The `/-/` prefix keeps our routes clear of table and database URLs.
- Handlers see every HTTP method. Return 405 for anything but POST.
- Return `Response.json(...)`, `Response.html(...)` or `Response.redirect(...)`.

`approval.py` already follows this shape.

## 2. Writes in POST handlers

```python
db = datasette.get_database("operations")   # name = db filename stem
await db.execute_write("UPDATE t SET x = ? WHERE id = ?", [x, id])
```

- Always bind parameters (`?`). Never f-string user input into SQL.
- Each `execute_write` is its own transaction, run on Datasette's single write
  thread. Two calls are not atomic together.
- For check-then-write, or several statements that must land together, use
  `execute_write_fn`. **In Datasette 0.65.5 it does NOT open a transaction or
  roll back for you** (it just calls your function on the write connection).
  Wrap the body in `with conn:` to get commit on success and rollback on any
  exception; the exception is re-raised to the `await` caller:

```python
def _decide(conn):
    with conn:
        n = conn.execute("UPDATE proposals SET status=? WHERE proposal_id=? "
                         "AND status='PENDING_APPROVAL'", [new, pid]).rowcount
        if n != 1:
            raise ValueError("already decided")
        conn.execute("INSERT INTO approvals (...) VALUES (...)", [...])

await db.execute_write_fn(_decide)          # block=True by default
```

- The `WHERE status = 'PENDING_APPROVAL'` guard plus a `rowcount` check is what
  makes a double-click harmless. Reading the status first and writing later is
  a race.

## 3. Templates, data, and metadata wiring

Templates are loaded by name from `--template-dir`. **They do not fetch their
own data.** The handler queries, then passes a context dict:

```python
rows = (await db.execute("SELECT ... FROM v_pending_approval")).rows
html = await datasette.render_template(
    "approvals.html", {"rows": rows}, request=request)
return Response.html(html)
```

- Pass `request=request`, or `csrftoken()` is not available in the template.
- Query in Python, pass results in. Avoid `datasette-template-sql`, which puts
  SQL inside templates where it cannot be tested.
- Extend Datasette's layout with `{% extends "base.html" %}`; no CSS framework.

**Cross-site POSTs need our own guard.** Datasette's CSRF check (asgi-csrf)
only enforces on requests that carry cookies. We have no login and therefore
no cookies, so it does not protect us: any web page open in the browser could
`fetch("http://localhost:8001/-/approve-all", {method: "POST", mode: "no-cors"})`
and it would go through. Every route in `approval.py` is wrapped in
`same_origin_only`, which returns 403 unless `Sec-Fetch-Site` is `same-origin`
or `none` (when present) and `Origin` is absent or matches the `Host` header.
curl and scripts send neither header and still work. New POST routes must be
registered through the same wrapper.

**Network binding.** `datasette serve` binds to `127.0.0.1` by default (`-h`
defaults to it, port 8001), so only this machine can connect. Never pass
`-h 0.0.0.0`; the guard above assumes local-only access. Forms may still
include `<input type="hidden" name="csrftoken" value="{{ csrftoken() }}">` and
`fetch()` callers `X-CSRFToken`, which costs nothing and covers the
cookie-bearing case, but it is not the protection we rely on. A
`skip_csrf` plugin hook is never implemented.

**`metadata.json`** carries display config only: table descriptions and sort
order, canned `queries`, and per-plugin config under `"plugins"`. Read plugin
config in code with `datasette.plugin_config("plugin_name")`. Do not put
secrets there: it is served at `/-/metadata`. A `"settings"` block in
metadata is ignored in 0.65.

## Not ported

Authentication, sessions, roles, admin panels, the upload engine,
trash/soft-delete, and Docker/Fly.io/Procfile config. `actor_from_request`
is unnecessary; there is one user at the machine.
