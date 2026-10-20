# CoolAPI

Small local API client. Import an OpenAPI/Swagger spec, send requests, keep your saved values when you import again.

## Run

```
uv run coolapi.py [port]     # default 8765, opens the browser
uv run test_coolapi.py       # tests
```

Needs `uv`. Only dependency: `pyyaml` (inline in the script). The server listens on `127.0.0.1` only.

## Use

- **Import**: spec URL, file or pasted text. JSON and YAML, OpenAPI 3 and Swagger 2.
- **Import again**: use the same collection name. The spec part is replaced. Saved path/query/header values and bodies are kept, matched by `operationId` (or method + path). Requests you added with `+ Request` are kept. Requests removed from the spec are dropped.
- **Send**: `Ctrl/Cmd+Enter`. The server makes the call, so there is no CORS. Redirects are not followed.
- **Query params**: use `+` on a row to repeat a param (`?id=1&id=2`).
- **Env** button: variables (`{{name}}` works in URL, headers, body), OAuth2 settings, default headers and query for all requests.

On first import the spec seeds `base_url`, variables for its security schemes, default auth headers, and a default `Accept` header. Import sets `Accept` again if all 2xx responses use one media type.

## OAuth2

Set an `auth` block in an env (the Env dialog has a form for it): `token_url`, `client_id`, `client_secret`, `scope`, `style` (`basic` or `body`). The server gets a `client_credentials` token, keeps it in memory, refreshes it before it expires, and retries once on 401. It replaces any default `Authorization` header. The **Token** button forces a refresh.

## Files

```
coolapi.py        server, spec import and merge, send, OAuth2
index.html        UI (no build step)
test_coolapi.py   tests
data/<name>.json      collection: requests, saved values, default headers
data/<name>.env.json  envs and credentials
```

`data/` is gitignored: it holds your saved bodies, values and credentials.

## Not included

Scripts and tests in requests, history, cookies, file upload, OAuth flows that need a browser redirect, signature auth.
