"""Entity and relationship extraction from chunks.

For each chunk, ask Claude to extract entities and relationships as structured JSON.
This is the single biggest cost driver in the pipeline. We use prompt caching
on the system message to amortize the instruction tokens across many calls.
"""
import json
import time
from dataclasses import dataclass, asdict
from typing import List, Optional, Tuple
import pickle
from pathlib import Path

from anthropic import Anthropic
from tqdm import tqdm

from config import (
    require_api_key, EXTRACTION_MODEL, CACHE_DIR,
)
from chunking import Chunk


client = Anthropic(api_key=require_api_key())


@dataclass
class Entity:
    name: str           # canonical name
    entity_type: str    # Person | Company | Product | Material | Event | Location | Organization
    description: str    # one-sentence description
    chunk_id: str       # source chunk
    aliases: List[str] = None  # acronyms and short forms found in this chunk

    def __post_init__(self):
        if self.aliases is None:
            self.aliases = []


@dataclass
class Relationship:
    source: str         # entity name
    target: str         # entity name
    rel_type: str       # SUPPLIES, COMPETES_WITH, EMPLOYS, ACQUIRED, ENDORSES, etc.
    description: str    # context for the relationship
    chunk_id: str       # source chunk


SYSTEM_PROMPT = """You are an information extraction system that identifies entities and relationships from text for a knowledge graph.

Extract ENTITIES (people, companies, products, materials, events, locations, organizations) and RELATIONSHIPS between them.

Rules for entities:
- Use canonical full names (e.g., "International Tennis Federation" not "ITF", "Apex Racquets, Inc." not "Apex")
- For people, use full name when available (e.g., "Klaus Reinhardt")
- entity_type must be one of: Person, Company, Product, Material, Event, Location, Organization, Technology
- Include a one-sentence description grounded ONLY in this chunk's content
- In the aliases field, list all alternative names, acronyms, or short forms for this entity that appear in the chunk (e.g., "ITF" for "International Tennis Federation", "WSJ" for "Wall Street Journal"). If none appear, use an empty list.
- Skip generic concepts and common nouns
- Skip pronouns and references that don't name a specific entity

Rules for relationships:
- Both source and target must be entities you've extracted
- rel_type should be UPPERCASE_SNAKE_CASE (e.g., SUPPLIES, COMPETES_WITH, ACQUIRED, EMPLOYS, ENDORSES, MANUFACTURES, LOCATED_IN, USES, OWNS, FOUNDED, REPLACED)
- Only extract relationships explicitly stated or strongly implied by the text
- Do not extract relationships you're inferring from outside knowledge
- description should quote or closely paraphrase the supporting text

Return ONLY valid JSON with this exact schema:
{
  "entities": [
    {"name": "string (canonical full name)", "aliases": ["string array of alternative names, acronyms, or short forms used in this chunk"], "entity_type": "string", "description": "string"}
  ],
  "relationships": [
    {"source": "string", "target": "string", "rel_type": "string", "description": "string"}
  ]
}

Examples:
  {"name": "International Tennis Federation", "aliases": ["ITF"], "entity_type": "Organization", "description": "..."}
  {"name": "Wall Street Journal", "aliases": ["WSJ"], "entity_type": "Organization", "description": "..."}
  {"name": "Apex Racquets, Inc.", "aliases": ["Apex"], "entity_type": "Company", "description": "..."}

No markdown, no code fences, no commentary. JSON only."""


def extract_from_chunk(chunk: Chunk, max_retries: int = 3) -> Tuple[List[Entity], List[Relationship]]:
    """Extract entities and relationships from a single chunk."""
    user_message = f"Extract entities and relationships from the following text:\n\n---\n\n{chunk.content}"

    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=EXTRACTION_MODEL,
                max_tokens=2048,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},  # cache the long instructions
                    }
                ],
                messages=[{"role": "user", "content": user_message}],
            )
            text = response.content[0].text.strip()

            # Strip code fences if model added them despite instructions
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
                if text.endswith("```"):
                    text = text.rsplit("\n", 1)[0]

            data = json.loads(text)

            entities = [
                Entity(
                    name=e["name"].strip(),
                    entity_type=e["entity_type"].strip(),
                    description=e["description"].strip(),
                    chunk_id=chunk.chunk_id,
                    aliases=[a.strip() for a in e.get("aliases", []) if isinstance(a, str) and a.strip()],
                )
                for e in data.get("entities", [])
                if e.get("name")
            ]

            relationships = [
                Relationship(
                    source=r["source"].strip(),
                    target=r["target"].strip(),
                    rel_type=r["rel_type"].strip(),
                    description=r["description"].strip(),
                    chunk_id=chunk.chunk_id,
                )
                for r in data.get("relationships", [])
                if r.get("source") and r.get("target")
            ]

            return entities, relationships

        except (json.JSONDecodeError, KeyError, IndexError) as e:
            if attempt == max_retries - 1:
                print(f"  ⚠️  Extraction failed for {chunk.chunk_id}: {e}")
                return [], []
            time.sleep(1)
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"  ⚠️  API error for {chunk.chunk_id}: {e}")
                return [], []
            time.sleep(2 ** attempt)

    return [], []


def extract_all(chunks: List[Chunk], use_cache: bool = True) -> Tuple[List[Entity], List[Relationship]]:
    """Run extraction over all chunks with caching."""
    cache_path = CACHE_DIR / "extraction.pkl"

    if use_cache and cache_path.exists():
        print(f"Loading cached extractions from {cache_path}")
        with open(cache_path, "rb") as f:
            return pickle.load(f)

    all_entities: List[Entity] = []
    all_relationships: List[Relationship] = []

    print(f"Extracting from {len(chunks)} chunks...")
    for chunk in tqdm(chunks, desc="Extracting"):
        entities, relationships = extract_from_chunk(chunk)
        all_entities.extend(entities)
        all_relationships.extend(relationships)

    total_aliases = sum(len(e.aliases) for e in all_entities)
    print(f"Extracted {len(all_entities)} entities, {len(all_relationships)} relationships, {total_aliases} aliases")

    with open(cache_path, "wb") as f:
        pickle.dump((all_entities, all_relationships), f)

    return all_entities, all_relationships


if __name__ == "__main__":
    from chunking import load_corpus
    chunks = load_corpus()
    # Test on just first 3 chunks
    entities, rels = [], []
    for chunk in chunks[:3]:
        e, r = extract_from_chunk(chunk)
        entities.extend(e)
        rels.extend(r)
    print(f"\nExtracted {len(entities)} entities, {len(rels)} relationships")
    for e in entities[:5]:
        print(f"  Entity: {e.name} ({e.entity_type}) - {e.description[:80]}")
    for r in rels[:5]:
        print(f"  Rel: {r.source} -[{r.rel_type}]-> {r.target}")
