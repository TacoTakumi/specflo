"""LF-delimited JSONL framing for the pi RPC protocol.

Strict JSONL semantics per pi docs/rpc.md: LF (0x0A) is the only record
delimiter; a trailing CR is stripped; U+2028/U+2029 are valid inside JSON
strings and must never split a record. Splitting happens at the byte level on
0x0A, which the UTF-8 encodings of U+2028/U+2029 cannot contain.

Stdlib only - the agent subsystem imports nothing from pipeline code (REQ-15).
"""

from __future__ import annotations

import json
from typing import Any, BinaryIO, Iterator


def encode_frame(obj: Any) -> bytes:
    """Encode one object as a compact, LF-terminated JSON line.

    ensure_ascii=False mirrors pi's own JSON.stringify output, so U+2028 and
    U+2029 pass through as raw characters. json.dumps always escapes control
    characters below 0x20, so the payload can never contain a raw LF or CR.
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    ) + b"\n"


def decode_frame(line: bytes | str) -> Any:
    """Decode a single frame; tolerates a trailing CR and/or LF."""
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    return json.loads(line.rstrip("\r\n"))


class FrameDecoder:
    """Incremental frame decoder: feed byte chunks, get decoded objects back.

    Buffers partial lines across feeds; blank lines are skipped.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Any]:
        self._buf += data
        frames: list[Any] = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                return frames
            line = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if line:
                frames.append(json.loads(line.decode("utf-8")))


def read_frames(stream: BinaryIO) -> Iterator[Any]:
    """Yield frames from a binary stream until EOF; blocks between frames."""
    decoder = FrameDecoder()
    while True:
        chunk = stream.read1(65536)
        if not chunk:
            return
        yield from decoder.feed(chunk)


def write_frame(stream: BinaryIO, obj: Any) -> None:
    """Write one frame and flush."""
    stream.write(encode_frame(obj))
    stream.flush()
