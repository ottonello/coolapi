# CoolAPI

Small local API client. Import an OpenAPI/Swagger spec, send requests, keep your saved values when you import again.

## Run

```
uv run coolapi.py [port]     # default 8765, opens the browser
uv run test_coolapi.py       # tests
```

Needs `uv`. Dependencies: `pyyaml` and `jq` (inline in the script, `uv` installs them). The server listens on `127.0.0.1` only.

## Use

- **Import**: spec URL, file or pasted text. JSON and YAML, OpenAPI 3 and Swagger 2.
- **Import again**: use the same collection name. The spec part is replaced. Saved path/query/header values and bodies are kept, matched by `operationId` (or method + path). Requests you added with `+ Request` are kept. Requests removed from the spec are dropped.
- **Request filter**: the field above the request list does fuzzy search on method, path, summary and `operationId`. Characters must appear in order, with gaps allowed, so `usrid` finds `/users/{id}`. Separate terms with spaces to match across fields (`get pets`). Best matches come first in each tag, and matched characters are highlighted.
- **Send**: `Ctrl/Cmd+Enter`. The server makes the call, so there is no CORS. Redirects are not followed.
- **Response**: JSON is highlighted, and objects and arrays fold with the arrow next to them.
- **Filter**: the field above a JSON response takes a [jq](https://jqlang.org/manual/) filter, for example `.items[].id`, and runs as you type. It stays on the request while the app is open and applies again on each send. A filter is stopped after 5 seconds or 10 MB of output.
- **Query params**: use `+` on a row to repeat a param (`?id=1&id=2`).
- **History** tab: every sent request, newest first, with the response. Click one to see it, `Open request` jumps to it. `Clear` deletes the history of the collection.
- **Env** button: variables (`{{name}}` works in URL, headers, body), OAuth2 settings, default headers and query for all requests.

On first import the spec seeds `base_url`, variables for its security schemes, default auth headers, and a default `Accept` header. Import sets `Accept` again if all 2xx responses use one media type.

## OAuth2

Set an `auth` block in an env (the Env dialog has a form for it): `token_url`, `client_id`, `client_secret`, `scope`, `style` (`basic` or `body`). The server gets a `client_credentials` token, keeps it in memory, refreshes it before it expires, and retries once on 401. It replaces any default `Authorization` header. The **Token** button forces a refresh.

## History

Stored in `data/<name>.history.jsonl`. The request is saved as written, with `{{vars}}` not resolved, so env secrets are not stored. Header values with auth, key, token, secret, cookie or pass in the name are hidden unless they are `{{vars}}`. Response bodies are cut at 100 KB, but they can still contain secrets the API returns. The file keeps its newest half when it goes over 20 MB.

## Files

```
coolapi.py        server, spec import and merge, send, OAuth2
index.html        UI (no build step)
test_coolapi.py   tests
data/<name>.json      collection: requests, saved values, default headers
data/<name>.env.json  envs and credentials
data/<name>.history.jsonl  sent requests and responses
```

`data/` is gitignored: it holds your saved bodies, values and credentials.

## Not included

Scripts and tests in requests, cookies, file upload, OAuth flows that need a browser redirect, signature auth.
