#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pyyaml"]
# ///
"""coolapi: local API client. Import OpenAPI/Swagger, keep saved values and creds on re-import."""
import base64
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

import yaml

ROOT = Path(__file__).parent
DATA = ROOT / "data"
PORT = 8765
METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
EMPTY_SAVED = {"path": {}, "query": {}, "headers": {}, "body": ""}
VAR = re.compile(r"\{\{\s*([\w.-]+)\s*\}\}")


# --- spec parsing ---


def parse_spec(text):
    try:
        spec = json.loads(text)
    except ValueError:
        spec = yaml.safe_load(text)
    if not isinstance(spec, dict) or not ("openapi" in spec or "swagger" in spec):
        raise ValueError("not an OpenAPI/Swagger spec")
    return spec


def deref(spec, node):
    # local $ref only
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if not ref.startswith("#/"):
            return {}
        node = spec
        for part in ref[2:].split("/"):
            node = node.get(part.replace("~1", "/").replace("~0", "~"), {})
    return node


def sample(spec, schema, depth=0):
    schema = deref(spec, schema)
    if not isinstance(schema, dict) or depth > 4:
        return None
    for k in ("example", "default"):
        if k in schema:
            return schema[k]
    if schema.get("enum"):
        return schema["enum"][0]
    if "allOf" in schema:
        out = {}
        for s in schema["allOf"]:
            v = sample(spec, s, depth + 1)
            if isinstance(v, dict):
                out.update(v)
        return out
    for k in ("oneOf", "anyOf"):
        if schema.get(k):
            return sample(spec, schema[k][0], depth + 1)
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        return {
            k: sample(spec, v, depth + 1)
            for k, v in (schema.get("properties") or {}).items()
        }
    if t == "array":
        return [sample(spec, schema.get("items", {}), depth + 1)]
    if t == "integer" or t == "number":
        return 0
    if t == "boolean":
        return False
    return {"date-time": "2024-01-01T00:00:00Z", "date": "2024-01-01"}.get(
        schema.get("format"), "string"
    )


def body_skeleton(spec, op):
    schema = None
    media = None
    rb = deref(spec, op.get("requestBody", {}))
    content = rb.get("content") or {}
    key = next((k for k in content if "json" in k), None)
    if key:
        media = content[key]
        schema = media.get("schema")
    for p in op.get("parameters", []):  # swagger 2
        p = deref(spec, p)
        if p.get("in") == "body":
            schema = p.get("schema")
    if media and "example" in media:
        return json.dumps(media["example"], indent=2)
    if schema is None:
        return ""
    return json.dumps(sample(spec, schema), indent=2)


def spec_ops(spec):
    out = {}
    for path, item in (spec.get("paths") or {}).items():
        item = deref(spec, item)
        for m in METHODS:
            op = item.get(m)
            if not isinstance(op, dict):
                continue
            params = {}
            for p in item.get("parameters", []) + op.get("parameters", []):
                p = deref(spec, p)
                if p.get("in") in ("query", "header", "path"):
                    params[(p["name"], p["in"])] = {
                        "name": p["name"],
                        "in": p["in"],
                        "required": bool(p.get("required")),
                    }
            key = op.get("operationId") or f"{m.upper()} {path}"
            if key in out:
                key = f"{m.upper()} {path}"
            out[key] = {
                "method": m.upper(),
                "path": path,
                "summary": op.get("summary", ""),
                "tag": (op.get("tags") or ["other"])[0],
                "params": list(params.values()),
                "body_default": body_skeleton(spec, op),
            }
    return out


def base_url(spec, source=""):
    if spec.get("servers"):
        s = spec["servers"][0]
        url = s.get("url", "")
        for k, v in (s.get("variables") or {}).items():
            url = url.replace("{%s}" % k, str(v.get("default", "")))
        if source and not urlparse(url).scheme:
            url = urljoin(source, url)
        return url.rstrip("/")
    if spec.get("host"):
        scheme = (spec.get("schemes") or ["https"])[0]
        return f"{scheme}://{spec['host']}{spec.get('basePath', '')}".rstrip("/")
    return ""


def security(spec):
    """Default headers/query/env vars from the spec's security schemes."""
    defs = (
        spec.get("components", {}).get("securitySchemes")
        or spec.get("securityDefinitions")
        or {}
    )
    headers, query, env = {}, {}, {}
    for name, d in defs.items():
        d = deref(spec, d)
        var = re.sub(r"\W", "_", name)
        t = d.get("type")
        cc = (d.get("flows") or {}).get("clientCredentials") or (
            d if d.get("flow") == "application" else None
        )
        if t == "oauth2" and cc and cc.get("tokenUrl"):
            env["auth"] = {
                "type": "oauth2_cc",
                "token_url": cc["tokenUrl"],
                "client_id": "",
                "client_secret": "",
                "scope": " ".join(cc.get("scopes") or {}),
                "style": "basic",
            }
            continue
        if t == "apiKey" and d.get("in") in ("header", "query"):
            (headers if d["in"] == "header" else query)[d["name"]] = "{{%s}}" % var
        elif t in ("http", "basic", "oauth2", "openIdConnect"):
            basic = t == "basic" or d.get("scheme") == "basic"
            headers["Authorization"] = "%s {{%s}}" % (
                "Basic" if basic else "Bearer",
                var,
            )
        else:
            continue
        env[var] = ""
    return headers, query, env


def accept_type(spec):
    """The one media type all 2xx responses use, else None."""
    types = set(spec.get("produces") or [])  # swagger 2
    for item in (spec.get("paths") or {}).values():
        item = deref(spec, item)
        for m in METHODS:
            op = item.get(m)
            if not isinstance(op, dict):
                continue
            types |= set(op.get("produces") or [])
            for code, r in (op.get("responses") or {}).items():
                if str(code).startswith("2"):
                    types |= set(deref(spec, r).get("content") or {})
    if len(types) == 1 and "*" not in (t := next(iter(types))):
        return t


def merge(old, ops):
    """New spec ops + saved values from old. Match by key, then by method+path."""
    old_reqs = old.get("requests", {})
    by_route = {(r["method"], r["path"]): r for r in old_reqs.values()}
    out = {}
    for k, op in ops.items():
        prev = old_reqs.get(k) or by_route.get((op["method"], op["path"])) or {}
        out[k] = {
            **op,
            "saved": prev.get("saved") or json.loads(json.dumps(EMPTY_SAVED)),
            "custom": False,
        }
    for k, r in old_reqs.items():
        if r.get("custom"):
            out[k] = r
    return out


# --- variables / http ---


def subst(s, env, missing):
    def rep(m):
        if m.group(1) in env:
            return str(env[m.group(1)])
        missing.add(m.group(1))
        return m.group(0)

    return VAR.sub(rep, s)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # don't forward credentials to a redirect target; show the 3xx instead
    def redirect_request(self, *a, **kw):
        return None


def http(method, url, headers, body, insecure=False):
    low = {k.lower() for k in headers}
    if body and "content-type" not in low:
        headers["Content-Type"] = "application/json"
    if "user-agent" not in low:
        headers["User-Agent"] = "coolapi"
    req = urllib.request.Request(
        url, body.encode() if body else None, headers, method=method
    )
    handlers = [NoRedirect]
    if insecure:
        handlers.append(
            urllib.request.HTTPSHandler(context=ssl._create_unverified_context())
        )
    opener = urllib.request.build_opener(*handlers)
    t0 = time.time()
    try:
        r = opener.open(req, timeout=60)
    except urllib.error.HTTPError as e:
        r = e
    except urllib.error.URLError as e:
        raise ValueError(str(e.reason))
    raw = r.read()
    return {
        "status": r.status if hasattr(r, "status") else r.code,
        "reason": r.reason,
        "url": url,
        "headers": list(r.headers.items()),
        "body": raw.decode("utf-8", "replace"),
        "ms": int((time.time() - t0) * 1000),
        "size": len(raw),
    }


# --- oauth2 client_credentials ---

TOKEN_SKEW = 30
_tokens = {}  # (collection, env, client_id) -> (token, expires_at); memory only
_token_lock = threading.Lock()  # ponytail: global lock, per-key locks if it matters


def env_vars(collection, env_name):
    env = load_env(collection)
    name = env_name or env["active"]
    return name, env["envs"].get(name, {})


def token_for(collection, env_name, force=False, insecure=False):
    """(token, expires_at) from the env's `auth` block, or None when it has none."""
    name, vars_ = env_vars(collection, env_name)
    auth = vars_.get("auth")
    if not auth:
        return None
    missing = set()
    a = {k: subst(str(v), vars_, missing) for k, v in auth.items()}
    if missing:
        raise ValueError("Unresolved variables: " + ", ".join(sorted(missing)))
    key = (collection, name, a.get("client_id", ""))
    with _token_lock:
        cached = _tokens.get(key)
        if cached and not force and time.time() < cached[1] - TOKEN_SKEW:
            return cached
        form = {"grant_type": "client_credentials"}
        if a.get("scope"):
            form["scope"] = a["scope"]
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if a.get("style", "basic") == "basic":
            cred = f"{a.get('client_id', '')}:{a.get('client_secret', '')}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(cred).decode()
        else:
            form["client_id"] = a.get("client_id", "")
            form["client_secret"] = a.get("client_secret", "")
        r = http("POST", a["token_url"], headers, urlencode(form), insecure)
        try:
            j = json.loads(r["body"])
        except ValueError:
            j = {}
        if r["status"] >= 400 or "access_token" not in j:
            raise ValueError(f"token request failed: {r['status']} {r['body'][:200]}")
        _tokens[key] = (j["access_token"], time.time() + int(j.get("expires_in", 3600)))
        return _tokens[key]


def send(p):
    name, vars_ = env_vars(p["collection"], p.get("env"))
    missing = set()
    f = lambda s: subst(s, vars_, missing)  # noqa: E731
    url = f(p["url"])
    headers = {f(k): f(v) for k, v in (p.get("headers") or {}).items()}
    body = f(p.get("body") or "")
    if missing:
        raise ValueError("Unresolved variables: " + ", ".join(sorted(missing)))
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"Bad URL (set base_url in the env): {url}")
    insecure = bool(p.get("insecure"))
    tok = token_for(p["collection"], p.get("env"), insecure=insecure)
    for attempt in (0, 1):
        h = dict(headers)
        if tok:  # env auth wins over any default Authorization header
            h = {k: v for k, v in h.items() if k.lower() != "authorization"}
            h["Authorization"] = "Bearer " + tok[0]
        out = http(p["method"], url, h, body, insecure)
        if out["status"] != 401 or not tok or attempt:
            break
        tok = token_for(p["collection"], p.get("env"), force=True, insecure=insecure)
    if tok:
        out["token_ttl"] = int(tok[1] - time.time())
    return out


# --- storage ---


def check_name(name):
    if not re.fullmatch(r"[\w-]+", name or ""):
        raise ValueError("bad name (use letters, digits, - _)")
    return name


def write(path, obj):
    DATA.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def col_path(name):
    return DATA / f"{check_name(name)}.json"


def env_path(name):
    return DATA / f"{check_name(name)}.env.json"


def load_col(name):
    return json.loads(col_path(name).read_text())


def load_env(name):
    p = env_path(name)
    if p.exists():
        return json.loads(p.read_text())
    return {"active": "default", "envs": {"default": {"base_url": ""}}}


def do_import(p):
    name = p.get("name")
    old = load_col(name) if name and col_path(name).exists() else {}
    text, src = p.get("spec_text"), p.get("url") or old.get("source", "")
    if not text:
        if not src:
            raise ValueError("url or spec_text required")
        req = urllib.request.Request(src, headers={"User-Agent": "coolapi"})
        text = urllib.request.urlopen(req, timeout=30).read().decode()
    spec = parse_spec(text)
    name = check_name(
        name
        or re.sub(r"\W+", "-", spec.get("info", {}).get("title", "api"))
        .strip("-")
        .lower()
    )
    h, q, env_vars = security(spec)
    headers = old.get("headers", h)
    accept = accept_type(spec)
    if accept:  # spec wins over any existing Accept
        headers = {k: v for k, v in headers.items() if k.lower() != "accept"}
        headers["Accept"] = accept
    col = {
        "source": p.get("url") or old.get("source", ""),
        "headers": headers,
        "query": old.get("query", q),
        "requests": merge(old, spec_ops(spec)),
    }
    write(col_path(name), col)
    if not env_path(name).exists():  # never touch existing creds
        write(
            env_path(name),
            {
                "active": "default",
                "envs": {"default": {"base_url": base_url(spec, src), **env_vars}},
            },
        )
    return {"name": name, "collection": col}


# --- server ---


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj, ctype="application/json"):
        data = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self):
        # localhost-only proxy: block DNS rebinding and cross-site form posts
        if self.headers.get("Host") not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            return self.reply(403, {"error": "bad host"})
        if (
            self.command != "GET"
            and self.headers.get("Content-Type", "").split(";")[0] != "application/json"
        ):
            return self.reply(403, {"error": "json only"})
        path = urlparse(self.path).path
        try:
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length") or 0)) or "null"
            )
            if self.command == "GET" and path == "/":
                return self.reply(
                    200, (ROOT / "index.html").read_text(), "text/html; charset=utf-8"
                )
            if self.command == "POST" and path == "/api/import":
                return self.reply(200, do_import(body))
            if self.command == "POST" and path == "/api/send":
                return self.reply(200, send(body))
            if self.command == "POST" and path == "/api/token":
                t = token_for(body["collection"], body.get("env"), force=True)
                if not t:
                    raise ValueError('env has no "auth" block')
                return self.reply(200, {"expires_in": int(t[1] - time.time())})
            m = re.fullmatch(r"/api/(collections|env)(?:/([\w-]+))?", path)
            if not m:
                return self.reply(404, {"error": "not found"})
            kind, name = m.groups()
            if kind == "collections" and name is None:
                names = sorted(
                    p.stem
                    for p in DATA.glob("*.json")
                    if not p.name.endswith(".env.json")
                )
                return self.reply(200, names)
            if self.command == "GET":
                return self.reply(
                    200, load_col(name) if kind == "collections" else load_env(name)
                )
            if self.command == "PUT":
                write(col_path(name) if kind == "collections" else env_path(name), body)
                return self.reply(200, {"ok": True})
            self.reply(405, {"error": "method not allowed"})
        except FileNotFoundError:
            self.reply(404, {"error": "not found"})
        except Exception as e:  # surfaced in the UI
            self.reply(400, {"error": f"{type(e).__name__}: {e}"})

    do_GET = do_PUT = do_POST = handle_any


if __name__ == "__main__":
    PORT = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"coolapi on http://127.0.0.1:{PORT}")
    webbrowser.open(f"http://127.0.0.1:{PORT}")
    srv.serve_forever()
