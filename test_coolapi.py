#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pyyaml"]
# ///
import json

from coolapi import base_url, merge, parse_spec, security, spec_ops, subst

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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
