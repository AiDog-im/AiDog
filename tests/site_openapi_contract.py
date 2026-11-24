#!/usr/bin/env python3
"""Offline OpenAPI contract check against the hosted Pages handlers."""
import json
import subprocess
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
spec = json.loads((ROOT / "site/openapi.json").read_text(encoding="utf-8"))
assert spec["openapi"] == "3.1.0"
# Pages' file-based routing must leave the static site outside Functions.
# Reject middleware, catch-all/dynamic routes, custom Workers and route overrides.
site = ROOT / "site"
assert not any((site / f).exists() for f in ("_worker.js", "_routes.json"))
routes = {"/" + str(p.relative_to(site / "functions").with_suffix(""))
          for p in (site / "functions").rglob("*") if p.is_file()
          and p.name != "_ledger_lib.js"}
assert routes == set(spec["paths"]) == {"/api/stats", "/api/visits", "/api/ledger", "/api/health"}
subprocess.run(["node", "tests/site_readonly.mjs"], cwd=ROOT, check=True)
fixtures = json.loads(subprocess.check_output(
    ["node", "tests/site_openapi_fixture.mjs"], cwd=ROOT, text=True))

def deref(schema):
    if isinstance(schema, dict) and "$ref" in schema:
        _, _, name = schema["$ref"].rpartition("/")
        return deref(spec["components"]["schemas"][name])
    if isinstance(schema, dict):
        return {key: deref(value) for key, value in schema.items()}
    if isinstance(schema, list):
        return [deref(value) for value in schema]
    return schema

def validate(schema, body):
    schema = deref(schema)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(body)

for fixture in fixtures:
    operation = spec["paths"][fixture["path"]][fixture["method"]]
    response = operation["responses"][str(fixture["status"])]
    content = response["content"][fixture["mime"]]
    if fixture["mime"] == "application/json":
        validate(content["schema"], fixture["body"])
        if "example" in content:
            validate(content["schema"], content["example"])
    elif "example" in content:
        assert isinstance(fixture["body"], str)

report = spec["paths"]["/api/ledger"]["post"]["requestBody"]["content"]["application/json"]
validate(report["schema"], report["examples"]["minimal"]["value"])
print(f"PASS {len(fixtures)} real-handler outcomes validated against OpenAPI 3.1")
