"""Splits text into overlapping chunks on paragraph / sentence / word boundaries."""
from __future__ import annotations

import re

_WS = re.compile(r"[ \t\f\v]+")
_BLANKS = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "\n".join(_WS.sub(" ", line).strip() for line in text.split("\n"))
    return _BLANKS.sub("\n\n", text).strip()


def _cut_point(text: str, start: int, end: int) -> int:
    """Best place to end a chunk in text[start:end]: paragraph > line > sentence > word."""
    if end >= len(text):
        return len(text)
    window = text[start:end]
    floor = len(window) // 2  # never produce chunks shorter than half the target
    for sep in ("\n\n", "\n", ". ", "? ", "! ", "; ", " "):
        i = window.rfind(sep)
        if i >= floor:
            return start + i + len(sep)
    return end


def chunk_text(text: str, size: int = 1200, overlap: int = 200) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = _cut_point(text, start, start + size)
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        nxt = max(end - overlap, start + 1)
        # start the overlap on a word boundary
        sp = text.find(" ", nxt, end)
        start = sp + 1 if sp != -1 else nxt
    return chunks
