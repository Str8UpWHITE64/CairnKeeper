"""Records every request the game makes, so the protocol can be studied offline.

The official servers are gone, so we cannot observe real *responses*. We can
still observe every *request*, which pins down the client half of the protocol
exactly. Each exchange is appended to a JSONL file and also written out as a
readable transcript.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redact import scrub, scrub_headers


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Recorder:
    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.jsonl = self.dir / "exchanges.jsonl"
        self.transcript = self.dir / "transcript.txt"
        self._lock = threading.Lock()
        self.count = 0

    def record(
        self,
        *,
        method: str,
        url: str,
        req_headers: dict[str, str],
        req_body: bytes,
        status: int,
        resp_body: bytes,
        handler: str,
    ) -> None:
        entry: dict[str, Any] = {
            "ts": _utcnow(),
            "method": method,
            "url": url,
            "handler": handler,
            "status": status,
            "request_headers": scrub_headers(req_headers),
            "request_body": scrub(_decode(req_body)),
            "response_body": scrub(_decode(resp_body)),
        }
        with self._lock:
            self.count += 1
            with self.jsonl.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            with self.transcript.open("a", encoding="utf-8") as fh:
                fh.write(f"\n{'=' * 78}\n#{self.count}  {method} {url}\n")
                fh.write(f"handler={handler}  status={status}  at={entry['ts']}\n")
                fh.write(f"{'-' * 78}\nREQUEST\n")
                fh.write(_pretty(entry["request_body"]))
                fh.write(f"\n{'-' * 78}\nRESPONSE\n")
                fh.write(_pretty(entry["response_body"]))
                fh.write("\n")


def _decode(body: bytes) -> Any:
    if not body:
        return None
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return {"__binary__": body.hex()[:4096], "__len__": len(body)}
    try:
        return json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return text


def _pretty(value: Any) -> str:
    if value is None:
        return "(empty)"
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, ensure_ascii=False)
