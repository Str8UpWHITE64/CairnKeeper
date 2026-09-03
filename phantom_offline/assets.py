"""Archives the blobs referenced by server responses.

Temple layouts and phantom recordings do not travel in the JSON. The server
returns URLs on a separate CDN (``dungeons.wiby.net``) and the game fetches them
directly, which means they never pass through our proxy:

    "layoutDownloadURL": "https://dungeons.wiby.net/version_128/2024-02-23/
                          dungeon-701203-floor-2-layout-ygck77l0"

Those files are the irreplaceable part. A seed alone does not reproduce a
temple -- the layout blob carries the actual room graph
(``numWings``, ``randomCurrent``, ``chosenGuardian``, ``connections``), and each
phantom is a separate recording. When the CDN goes, they are gone.

So whenever a response is archived, every download URL in it is pulled and
stored. Fetching happens on a background worker so the game is never blocked
waiting for us.
"""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import urllib.request
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

# Response fields known to carry a blob URL.
URL_KEYS = frozenset({"layoutdownloadurl", "downloadurl", "runurl", "asseturl"})

USER_AGENT = "X-UnrealEngine-Agent"


def iter_urls(value: Any) -> Iterator[str]:
    """Yield every download URL anywhere in a decoded response body."""
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in URL_KEYS and isinstance(item, str) and item.strip():
                yield item
            else:
                yield from iter_urls(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_urls(item)


def local_name(url: str) -> str:
    """Stable, filesystem-safe name preserving the original path."""
    parsed = urlparse(url)
    path = parsed.path.lstrip("/") or "index"
    safe = path.replace("/", "__")
    # Guard against absurd names while keeping URLs distinguishable.
    if len(safe) > 150:
        digest = hashlib.sha256(url.encode()).hexdigest()[:12]
        safe = f"{safe[:130]}__{digest}"
    return safe


class AssetArchiver:
    """Downloads and stores blob URLs, once each, in the background."""

    def __init__(self, directory: Path, *, workers: int = 4, timeout: float = 60.0):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"
        self.timeout = timeout

        self._lock = threading.Lock()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._seen: set[str] = set()
        self._index: dict[str, dict[str, Any]] = self._load_index()
        self._seen.update(self._index)

        self.fetched = 0
        self.failed = 0

        self._threads = [
            threading.Thread(target=self._worker, daemon=True, name=f"asset-{i}")
            for i in range(workers)
        ]
        for t in self._threads:
            t.start()

    # ------------------------------------------------------------- index

    def _load_index(self) -> dict[str, dict[str, Any]]:
        if self.index_path.exists():
            try:
                return json.loads(self.index_path.read_text("utf-8"))
            except (ValueError, OSError):
                pass
        return {}

    def _save_index(self) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._index, indent=2), encoding="utf-8")
        tmp.replace(self.index_path)

    # ------------------------------------------------------------ public

    def harvest(self, body: Any) -> int:
        """Queue every download URL found in *body*. Returns how many are new."""
        added = 0
        for url in iter_urls(body):
            with self._lock:
                if url in self._seen:
                    continue
                self._seen.add(url)
            self._queue.put(url)
            added += 1
        return added

    def pending(self) -> int:
        return self._queue.qsize()

    def drain(self, timeout: float | None = None) -> None:
        """Block until queued downloads finish (used by the backfill tool)."""
        self._queue.join()
        if timeout is not None:
            for t in self._threads:
                t.join(timeout=0)

    # ------------------------------------------------------------ worker

    def _worker(self) -> None:
        while True:
            url = self._queue.get()
            if url is None:
                self._queue.task_done()
                return
            try:
                self._fetch(url)
            except Exception as exc:  # noqa: BLE001 - never kill the worker
                with self._lock:
                    self.failed += 1
                    self._index[url] = {"error": repr(exc)}
                    self._save_index()
            finally:
                self._queue.task_done()

    def _fetch(self, url: str) -> None:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=self.timeout) as resp:
            data = resp.read()
            content_type = resp.headers.get("Content-Type", "")

        name = local_name(url)
        (self.dir / name).write_bytes(data)

        with self._lock:
            self.fetched += 1
            self._index[url] = {
                "file": name,
                "bytes": len(data),
                "content_type": content_type,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            self._save_index()
