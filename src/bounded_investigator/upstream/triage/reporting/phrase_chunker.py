"""Natural short-phrase chunking for paced terminal output.

Chunking is presentation-only.  It prefers punctuation and common Chinese phrase
boundaries, falls back to a small display-length cap, and keeps ASCII identifiers
(such as API names / F-xxxx refs) intact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_PUNCT = "，。！？；：,;:!?"
_SENTENCE_RE = re.compile(r"[^，。！？；：,;:!?\n]+[，。！？；：,;:!?]?|\n")
_ATOM_RE = re.compile(
    r"(?:F-\d+|U-\d+|[A-Za-z_][A-Za-z0-9_.:/+\-]*|\d+(?:\.\d+)?|\s+|.)"
)
_PREFERRED_ENDINGS = (
    "后",
    "中",
    "上",
    "下",
    "前",
    "并",
    "且",
    "但",
    "而",
    "与",
    "为",
    "由",
    "将",
    "把",
    "向",
    "从",
    "通过",
    "作为",
)


@dataclass(frozen=True)
class PhraseChunker:
    target_chars: int = 12
    min_chars: int = 5
    max_chars: int = 16

    def split(self, text: str) -> list[str]:
        if not text:
            return []
        chunks: list[str] = []
        for segment in _SENTENCE_RE.findall(text):
            if segment == "\n":
                chunks.append(segment)
                continue
            punctuation = segment[-1] if segment and segment[-1] in _PUNCT else ""
            body = segment[:-1] if punctuation else segment
            body_chunks = self._split_body(body)
            if punctuation:
                if body_chunks:
                    body_chunks[-1] += punctuation
                else:
                    body_chunks = [punctuation]
            chunks.extend(chunk for chunk in body_chunks if chunk)
        return chunks

    def _split_body(self, body: str) -> list[str]:
        if not body:
            return []
        atoms = _ATOM_RE.findall(body)
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for atom in atoms:
            atom_len = _display_len(atom)
            if current and current_len + atom_len > self.max_chars:
                chunks.append("".join(current))
                current = []
                current_len = 0
            current.append(atom)
            current_len += atom_len
            joined = "".join(current)
            if current_len >= self.target_chars and self._good_boundary(joined):
                chunks.append(joined)
                current = []
                current_len = 0
            elif current_len >= self.max_chars:
                chunks.append(joined)
                current = []
                current_len = 0
        if current:
            chunks.append("".join(current))

        # Avoid tiny tail fragments when the previous chunk can absorb them safely.
        if len(chunks) >= 2 and _display_len(chunks[-1]) < self.min_chars:
            if _display_len(chunks[-2]) + _display_len(chunks[-1]) <= self.max_chars + 3:
                chunks[-2] += chunks[-1]
                chunks.pop()
        return chunks

    @staticmethod
    def _good_boundary(text: str) -> bool:
        stripped = text.rstrip()
        if not stripped:
            return False
        return stripped.endswith(_PREFERRED_ENDINGS) or text.endswith(" ")


def _display_len(text: str) -> int:
    # We only need a stable chunking heuristic, not exact terminal wcwidth.  Counting
    # characters keeps identifiers atomic and yields 5-16 character phrase blocks.
    return len(text)
