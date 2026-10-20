#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pyyaml"]
# ///
import base64
import json
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import coolapi
from coolapi import do_import, accept_type, base_url, merge, parse_spec, security, send, spec_ops, subst

OAS3 = {
    "openapi": "3.0.0",
    "servers": [{"url": "/api/v3"}],
    "components": {
        "schemas": {
            "Pet": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            }
        },
        "securitySchemes": {
            "key": {"type": "apiKey", "in": "header", "name": "X-Key"},
            "tok": {"type": "http", "scheme": "bearer"},
        },
    },
    "paths": {
        "/pets/{id}": {
            "parameters": [{"name": "id", "in": "path", "required": True}],
            "get": {
                "operationId": "getPet",
                "tags": ["pets"],
                "parameters": [{"name": "q", "in": "query"}],
            },
            "put": {
                "operationId": "putPet",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Pet"}
                        }
                    }
                },
            },
        }
    },
}


def test_ops():
    ops = spec_ops(OAS3)
    assert set(ops) == {"getPet", "putPet"}
    assert {p["name"] for p in ops["getPet"]["params"]} == {
        "id",
        "q",
    }  # path-level + op-level
    assert json.loads(ops["putPet"]["body_default"]) == {"name": "string", "age": 0}
    assert ops["getPet"]["body_default"] == ""


def test_merge_keeps_saved_and_custom():
    old = {
        "requests": {
            "getPet": {
                "method": "GET",
                "path": "/pets/{id}",
                "saved": {"path": {"id": "7"}, "query": {}, "headers": {}, "body": ""},
            },
            "gone": {"method": "GET", "path": "/gone", "saved": {"body": "x"}},
            "mine": {
                "method": "GET",
                "path": "/mine",
                "custom": True,
                "saved": {"body": "y"},
            },
        }
    }
    new = merge(old, spec_ops(OAS3))
    assert new["getPet"]["saved"]["path"] == {"id": "7"}
    assert "gone" not in new and new["mine"]["saved"]["body"] == "y"
    assert new["putPet"]["saved"]["body"] == ""


def test_merge_renamed_operation_id():
    old = {
        "requests": {
            "oldName": {
                "method": "GET",
                "path": "/pets/{id}",
                "saved": {"path": {"id": "9"}},
            }
        }
    }
    assert merge(old, spec_ops(OAS3))["getPet"]["saved"]["path"] == {"id": "9"}


def test_subst():
    missing = set()
    assert subst("a {{x}} {{ y }} {{z}}", {"x": 1, "y": "b"}, missing) == "a 1 b {{z}}"
    assert missing == {"z"}


def test_base_url():
    assert base_url(OAS3, "https://h.io/api/v3/openapi.json") == "https://h.io/api/v3"
    assert (
        base_url(
            {"swagger": "2.0", "host": "h.io", "basePath": "/v1", "schemes": ["http"]}
        )
        == "http://h.io/v1"
    )


def test_security_and_yaml():
    h, q, env = security(OAS3)
    assert h == {"X-Key": "{{key}}", "Authorization": "Bearer {{tok}}"} and env == {
        "key": "",
        "tok": "",
    }
    assert parse_spec("openapi: 3.0.0\npaths: {}")["openapi"] == "3.0.0"


def test_security_oauth2_cc_seeds_auth():
    spec = {"openapi": "3.0.0", "components": {"securitySchemes": {"o": {"type": "oauth2", "flows": {
        "clientCredentials": {"tokenUrl": "https://t/token", "scopes": {"a": "", "b": ""}}}}}}}
    h, q, env = security(spec)
    assert h == {} and env["auth"]["token_url"] == "https://t/token" and env["auth"]["scope"] == "a b"


def test_oauth2_send_cache_and_retry():
    state = {"valid": "tok-1", "issued": 0, "forms": [], "basic": []}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def out(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            state["forms"].append(form)
            state["basic"].append(self.headers.get("Authorization"))
            state["issued"] += 1
            self.out(200, {"access_token": f"tok-{state['issued']}", "expires_in": 3600})

        def do_GET(self):
            ok = self.headers.get("Authorization") == "Bearer " + state["valid"]
            self.out(200 if ok else 401, {"ok": ok})

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    coolapi.DATA = Path(tempfile.mkdtemp())
    auth = {"token_url": base + "/token", "client_id": "{{cid}}", "client_secret": "s3", "scope": "x", "style": "basic"}
    env = {"active": "t", "envs": {"t": {"cid": "me", "auth": auth}}}
    coolapi.write(coolapi.env_path("c"), env)
    req = {"collection": "c", "method": "GET", "url": base + "/api", "headers": {"Authorization": "Bearer stale"}}

    assert send(dict(req))["status"] == 200  # fetched, overrides default header
    assert state["basic"][0] == "Basic " + base64.b64encode(b"me:s3").decode()
    assert state["forms"][0]["grant_type"] == ["client_credentials"] and "client_id" not in state["forms"][0]
    assert send(dict(req))["status"] == 200 and state["issued"] == 1  # cached
    state["valid"] = "tok-2"
    r = send(dict(req))  # 401 -> new token -> retry
    assert r["status"] == 200 and state["issued"] == 2 and r["token_ttl"] > 3000

    auth["style"] = "body"
    auth["client_id"] = "me2"  # new cache key
    coolapi.write(coolapi.env_path("c"), env)
    state["valid"] = "tok-3"
    assert send(dict(req))["status"] == 200
    assert state["forms"][-1]["client_id"] == ["me2"] and state["basic"][-1] is None
    srv.shutdown()


def test_accept_type():
    ok = {"200": {"content": {"application/json": {}}}, "404": {"content": {"text/plain": {}}}}
    spec = {"openapi": "3.0.0", "paths": {"/a": {"get": {"responses": ok}}, "/b": {"post": {"responses": ok}}}}
    assert accept_type(spec) == "application/json"  # only 2xx counts
    spec["paths"]["/b"]["post"]["responses"]["201"] = {"content": {"application/pdf": {}}}
    assert accept_type(spec) is None  # mixed
    assert accept_type({"swagger": "2.0", "produces": ["application/json"], "paths": {}}) == "application/json"
    assert accept_type(OAS3) is None  # nothing declared


def test_reimport_adds_missing_accept_keeps_existing():
    spec = {"openapi": "3.0.0", "paths": {"/a": {"get": {"responses": {"200": {"content": {"application/json": {}}}}}}}}
    coolapi.DATA = Path(tempfile.mkdtemp())
    coolapi.write(coolapi.col_path("c"), {"headers": {"X-Key": "{{k}}"}, "requests": {}})
    h = do_import({"name": "c", "spec_text": json.dumps(spec)})["collection"]["headers"]
    assert h == {"X-Key": "{{k}}", "Accept": "application/json"}  # added to existing collection
    coolapi.write(coolapi.col_path("c"), {"headers": {"accept": "text/csv"}, "requests": {}})
    h = do_import({"name": "c", "spec_text": json.dumps(spec)})["collection"]["headers"]
    assert h == {"accept": "text/csv"}  # user value wins


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
