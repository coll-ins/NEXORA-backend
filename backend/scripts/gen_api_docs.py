"""Write docs/api.md from docs/openapi.yaml.

    python backend/scripts/gen_api_docs.py          # rewrite docs/api.md
    python backend/scripts/gen_api_docs.py --check  # exit 1 if it is out of date

The spec stays the source of truth. Examples are built from each schema (first
enum value, 1 for integers, a name-based string), so they show the shape and
field names, not real data.
"""

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "docs" / "openapi.yaml"
OUT = ROOT / "docs" / "api.md"
METHODS = ("get", "post", "put", "patch", "delete")

STRINGS = {
    "email": "member@example.com",
    "password": "a-long-passphrase-1",
    "phone": "+254700000000",
    "reference": "dep-2026-11-18-001",
}


def resolve(spec, node):
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"].lstrip("#/").split("/")
        node = spec
        for part in ref:
            node = node[part]
    return node


def example(spec, schema, name="", depth=0):
    schema = resolve(spec, schema)
    if depth > 4 or not isinstance(schema, dict):
        return None
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        return {
            key: example(spec, sub, key, depth + 1)
            for key, sub in schema.get("properties", {}).items()
        }
    if kind == "array":
        return [example(spec, schema.get("items", {}), name, depth + 1)]
    if kind == "integer":
        return 1
    if kind == "boolean":
        return True
    if kind == "number":
        return 1.0
    if schema.get("format") == "date-time":
        return "2026-11-18T09:00:00Z"
    if schema.get("format") == "date":
        return "2026-11-18"
    return STRINGS.get(name, name or "string")


def json_block(value):
    return "```json\n" + json.dumps(value, indent=2) + "\n```"


def clean(text):
    return re.sub(r"\n{2,}", "\n\n", (text or "").strip())


def params_of(spec, path_item, op):
    found = []
    for raw in [*path_item.get("parameters", []), *op.get("parameters", [])]:
        p = resolve(spec, raw)
        kind = resolve(spec, p.get("schema", {})).get("type", "string")
        found.append(f"`{p['name']}` ({p['in']}, {kind})")
    return found


def render(spec):
    lines = [
        "# NEXORA API reference",
        "",
        "Generated from [`openapi.yaml`](openapi.yaml) by "
        "`python backend/scripts/gen_api_docs.py`. Do not edit by hand: change "
        "the spec and regenerate. A test fails when this file is out of date.",
        "",
        "Examples show field names and types built from the schemas, not real "
        "data.",
        "",
        clean(spec["info"]["description"]),
        "",
    ]
    for path, item in spec["paths"].items():
        for method in METHODS:
            op = item.get(method)
            if not op:
                continue
            lines += [f"## {method.upper()} {path}", "", f"**{op['summary']}**", ""]
            lines += [clean(op.get("description")), ""]
            params = params_of(spec, item, op)
            if params:
                lines += ["Parameters: " + ", ".join(params), ""]
            body = op.get("requestBody", {}).get("content", {}).get("application/json")
            if body:
                sample = json_block(example(spec, body["schema"]))
                lines += ["Request body:", "", sample, ""]
            codes = op["responses"]
            ok = [c for c in codes if c.startswith("2")]
            for code in ok:
                resp = codes[code]
                content = resp.get("content", {}).get("application/json")
                lines.append(f"Response **{code}**: {resp['description']}")
                if content:
                    lines += ["", json_block(example(spec, content["schema"]))]
                lines.append("")
            fails = ", ".join(c for c in codes if not c.startswith("2"))
            lines += [f"Error statuses: {fails or 'none'}", ""]
    return "\n".join(lines).rstrip() + "\n"


def main():
    text = render(yaml.safe_load(SPEC.read_text()))
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text() != text:
            print("docs/api.md is out of date; run backend/scripts/gen_api_docs.py")
            sys.exit(1)
        return
    OUT.write_text(text)


if __name__ == "__main__":
    main()
