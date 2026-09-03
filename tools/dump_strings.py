"""Extract ASCII and UTF-16LE strings (with file offsets) from a binary.

Writes a TSV: offset<TAB>encoding<TAB>string
Used to analyze PhantomAbyss-Win64-Shipping.exe without re-reading 89MB each time.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PRINTABLE = set(range(0x20, 0x7F)) | {0x09}


def ascii_strings(data: bytes, min_len: int):
    start = None
    for i, b in enumerate(data):
        if b in PRINTABLE:
            if start is None:
                start = i
        else:
            if start is not None and i - start >= min_len:
                yield start, data[start:i].decode("ascii", "replace")
            start = None
    if start is not None and len(data) - start >= min_len:
        yield start, data[start:].decode("ascii", "replace")


def utf16_strings(data: bytes, min_len: int):
    """Scan both alignments so we don't miss odd-offset strings."""
    for align in (0, 1):
        start = None
        i = align
        n = len(data) - 1
        while i < n:
            lo, hi = data[i], data[i + 1]
            if hi == 0 and lo in PRINTABLE:
                if start is None:
                    start = i
            else:
                if start is not None and (i - start) // 2 >= min_len:
                    yield start, data[start:i].decode("utf-16-le", "replace")
                start = None
            i += 2
        if start is not None and (n - start) // 2 >= min_len:
            yield start, data[start:n].decode("utf-16-le", "replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("binary", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("-m", "--min-len", type=int, default=4)
    args = ap.parse_args()

    data = args.binary.read_bytes()
    print(f"read {len(data):,} bytes from {args.binary}", file=sys.stderr)

    seen_w: set[int] = set()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with args.out.open("w", encoding="utf-8", newline="\n") as fh:
        for off, s in utf16_strings(data, args.min_len):
            if off in seen_w:
                continue
            seen_w.add(off)
            fh.write(f"{off}\tW\t{s}\n")
            count += 1
        for off, s in ascii_strings(data, args.min_len):
            fh.write(f"{off}\tA\t{s}\n")
            count += 1

    print(f"wrote {count:,} strings to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
