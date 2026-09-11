#!/usr/bin/env python3
"""Strict, bounded parser for curl ``--dump-header`` canary evidence."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import NoReturn


MAX_HEADER_BYTES = 65_536
MAX_LINE_BYTES = 8_192
TOKEN = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
STATUS = re.compile(rb"^HTTP/(?:1\.[01]|2|3) [1-5][0-9]{2}(?: .*)?$")


def fail() -> NoReturn:
    raise SystemExit(1)


def parse(path: Path) -> list[dict[str, list[str]]]:
    try:
        if path.stat().st_size > MAX_HEADER_BYTES:
            fail()
        with path.open("rb") as handle:
            raw = handle.read(MAX_HEADER_BYTES + 1)
    except OSError:
        fail()
    if not raw or len(raw) > MAX_HEADER_BYTES or b"\x00" in raw:
        fail()
    raw = raw.replace(b"\r\n", b"\n")
    if b"\r" in raw:
        fail()
    blocks: list[dict[str, list[str]]] = []
    for raw_block in raw.split(b"\n\n"):
        if not raw_block:
            continue
        lines = raw_block.split(b"\n")
        if any(not line or len(line) > MAX_LINE_BYTES for line in lines):
            fail()
        if STATUS.fullmatch(lines[0]) is None or any(byte < 32 or byte > 126 for byte in lines[0]):
            fail()
        headers: dict[str, list[str]] = {}
        for line in lines[1:]:
            if line[:1] in {b" ", b"\t"} or b":" not in line:
                fail()
            raw_name, raw_value = line.split(b":", 1)
            if TOKEN.fullmatch(raw_name) is None:
                fail()
            try:
                name = raw_name.decode("ascii").lower()
                value = raw_value.strip(b" \t").decode("ascii")
            except UnicodeDecodeError:
                fail()
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                fail()
            headers.setdefault(name, []).append(value)
        blocks.append(headers)
    if not blocks:
        fail()
    return blocks


def main(argv: list[str]) -> int:
    if len(argv) not in {4, 5}:
        return 2
    mode, raw_path, raw_name = argv[1:4]
    try:
        encoded_name = raw_name.encode("ascii", "strict")
    except UnicodeEncodeError:
        return 2
    if TOKEN.fullmatch(encoded_name) is None:
        return 2
    blocks = parse(Path(raw_path))
    name = raw_name.lower()
    if mode == "absent" and len(argv) == 4:
        return 0 if all(name not in block for block in blocks) else 1
    if mode == "exact" and len(argv) == 5:
        values = blocks[-1].get(name, [])
        return 0 if values == [argv[4]] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
