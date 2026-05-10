"""Document chunking.

Strategy: split on paragraph boundaries (double newlines), pack chunks up to
target token size. Keeps semantic units intact, which matters for extraction quality.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import List
import re

from config import CHUNK_SIZE_TOKENS, CHUNK_OVERLAP_TOKENS, CORPUS_DIR


@dataclass
class Chunk:
    chunk_id: str       # e.g., "01_apex_10k_risk_factors_chunk_0"
    doc_id: str         # e.g., "01_apex_10k_risk_factors"
    content: str
    position: int       # ordinal within doc


def estimate_tokens(text: str) -> int:
    """Rough token estimate: 1 token ≈ 4 characters. Good enough for chunking."""
    return len(text) // 4


def split_into_paragraphs(text: str) -> List[str]:
    """Split on blank lines, preserving headers and lists as their own paragraphs."""
    # Normalize line endings
    text = text.replace("\r\n", "\n")
    # Split on 2+ newlines
    paragraphs = re.split(r"\n\s*\n", text)
    return [p.strip() for p in paragraphs if p.strip()]


def chunk_document(doc_id: str, content: str) -> List[Chunk]:
    """Pack paragraphs into chunks of target size."""
    paragraphs = split_into_paragraphs(content)
    chunks: List[Chunk] = []
    current_paras: List[str] = []
    current_tokens = 0
    position = 0

    for para in paragraphs:
        para_tokens = estimate_tokens(para)

        # If a single paragraph exceeds the chunk size, emit it on its own
        if para_tokens > CHUNK_SIZE_TOKENS:
            if current_paras:
                chunks.append(Chunk(
                    chunk_id=f"{doc_id}_chunk_{position}",
                    doc_id=doc_id,
                    content="\n\n".join(current_paras),
                    position=position,
                ))
                position += 1
                current_paras = []
                current_tokens = 0
            chunks.append(Chunk(
                chunk_id=f"{doc_id}_chunk_{position}",
                doc_id=doc_id,
                content=para,
                position=position,
            ))
            position += 1
            continue

        # If adding this paragraph would exceed the limit, emit current chunk
        if current_tokens + para_tokens > CHUNK_SIZE_TOKENS and current_paras:
            chunks.append(Chunk(
                chunk_id=f"{doc_id}_chunk_{position}",
                doc_id=doc_id,
                content="\n\n".join(current_paras),
                position=position,
            ))
            position += 1
            # Overlap: carry the last paragraph forward
            if CHUNK_OVERLAP_TOKENS > 0 and current_paras:
                last_para = current_paras[-1]
                if estimate_tokens(last_para) <= CHUNK_OVERLAP_TOKENS * 2:
                    current_paras = [last_para]
                    current_tokens = estimate_tokens(last_para)
                else:
                    current_paras = []
                    current_tokens = 0
            else:
                current_paras = []
                current_tokens = 0

        current_paras.append(para)
        current_tokens += para_tokens

    # Flush remainder
    if current_paras:
        chunks.append(Chunk(
            chunk_id=f"{doc_id}_chunk_{position}",
            doc_id=doc_id,
            content="\n\n".join(current_paras),
            position=position,
        ))

    return chunks


def load_corpus() -> List[Chunk]:
    """Read all .md files from the corpus directory and chunk them."""
    all_chunks: List[Chunk] = []
    for path in sorted(Path(CORPUS_DIR).glob("*.md")):
        doc_id = path.stem
        content = path.read_text(encoding="utf-8")
        all_chunks.extend(chunk_document(doc_id, content))
    return all_chunks


if __name__ == "__main__":
    chunks = load_corpus()
    print(f"Total chunks: {len(chunks)}")
    print(f"Documents: {len(set(c.doc_id for c in chunks))}")
    print(f"Avg chunk tokens: {sum(estimate_tokens(c.content) for c in chunks) / len(chunks):.0f}")
    print(f"\nFirst chunk preview:\n{chunks[0].content[:300]}...")
