"""Token-bounded section chunks with exact normalized-text character offsets."""

import hashlib
from typing import Any

from app.indexing.embeddings import Encoder


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def chunk_document(
    parsed: dict[str, Any], encoder: Encoder, budget: int, overlap: int
) -> tuple[str, list[dict[str, Any]]]:
    if not 0 <= overlap < budget <= encoder.max_tokens:
        raise ValueError("Invalid chunk budget/overlap for selected model")
    text = parsed["text"]
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Empty parsed document")
    version = "section-token-v1:" + digest(f"{encoder.key}:{budget}:{overlap}")[:24]
    headings = parsed.get("headings", [])
    boundaries = [(0, None)]
    for heading in headings:
        start = heading["start"]
        if not isinstance(start, int) or not 0 <= start < len(text):
            raise ValueError("Invalid section offset")
        if start < boundaries[-1][0]:
            raise ValueError("Unordered section offsets")
        if start == boundaries[-1][0]:
            boundaries[-1] = (start, heading["heading"])
        else:
            boundaries.append((start, heading["heading"]))
    chunks: list[dict[str, Any]] = []
    for index, (start, section) in enumerate(boundaries):
        end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(text)
        offsets = encoder.offsets(text[start:end])
        position = 0
        while position < len(offsets):
            stop = min(position + budget, len(offsets))
            a, b = start + offsets[position][0], start + offsets[stop - 1][1]
            # Re-tokenization at a WordPiece boundary can add tokens. Shrink, never truncate.
            while len(encoder.offsets(text[a:b])) > budget and stop > position + 1:
                stop -= 1
                b = start + offsets[stop - 1][1]
            body = text[a:b]
            chunks.append(
                {
                    "ordinal": len(chunks),
                    "section": section,
                    "start_offset": a,
                    "end_offset": b,
                    "text": body,
                    "content_hash": digest(body),
                    "token_count": len(encoder.offsets(body)),
                }
            )
            if stop == len(offsets):
                break
            position = max(position + 1, stop - overlap)
    if not chunks:
        raise ValueError("No indexable text")
    return version, chunks
