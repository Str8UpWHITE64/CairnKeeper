"""Summarize captured fixtures: endpoints, status codes, and field shapes.

Turns the raw JSONL archive into a readable picture of the protocol so the
offline backend can be validated against what the real server actually sent.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        if not value:
            return "[]"
        return f"[{type_name(value[0])}]"
    if isinstance(value, dict):
        return "{...}"
    return type(value).__name__


def shape(value: Any, prefix: str = "", out: dict[str, str] | None = None, depth: int = 0):
    """Flatten a JSON value into dotted field -> type."""
    if out is None:
        out = {}
    if depth > 3:
        return out
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else key
            out[path] = type_name(item)
            if isinstance(item, dict):
                shape(item, path, out, depth + 1)
            elif isinstance(item, list) and item and isinstance(item[0], dict):
                shape(item[0], f"{path}[]", out, depth + 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixtures", type=Path, nargs="?", default=Path("run/fixtures"))
    ap.add_argument("--endpoint", help="show full detail for one endpoint")
    args = ap.parse_args()

    for path in sorted(args.fixtures.glob("*.jsonl")):
        name = path.stem
        if args.endpoint and args.endpoint.lower() not in name.lower():
            continue

        entries = []
        for line in path.read_text("utf-8").splitlines():
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    pass
        if not entries:
            continue

        statuses = Counter(e["status"] for e in entries)
        print(f"\n{'=' * 74}\n{name}  ({len(entries)} call(s), status {dict(statuses)})")

        req_fields: dict[str, set[str]] = defaultdict(set)
        resp_fields: dict[str, set[str]] = defaultdict(set)
        for e in entries:
            if e["status"] != 200:
                continue
            for field, typ in shape(e.get("request_body") or {}).items():
                req_fields[field].add(typ)
            for field, typ in shape(e.get("response_body") or {}).items():
                resp_fields[field].add(typ)

        for label, fields in (("REQUEST", req_fields), ("RESPONSE", resp_fields)):
            if not fields:
                continue
            print(f"\n  {label}")
            for field in sorted(fields):
                types = "|".join(sorted(fields[field]))
                indent = "    " + "  " * field.count(".")
                print(f"{indent}{field.split('.')[-1]}: {types}")

        if args.endpoint:
            ok = [e for e in entries if e["status"] == 200]
            sample = ok[-1] if ok else entries[-1]
            print(f"\n  SAMPLE (status {sample['status']})")
            print("  request :", json.dumps(sample.get("request_body"))[:1500])
            print("  response:", json.dumps(sample.get("response_body"))[:2500])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
