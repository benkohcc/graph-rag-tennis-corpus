"""Query routing: three modes — combined, combined_community, global."""
import json
from typing import Literal

from anthropic import Anthropic

from config import require_api_key, ROUTING_MODEL


client = Anthropic(api_key=require_api_key())


ROUTING_PROMPT = """Classify the following question into one of three retrieval modes.

COMBINED — The question names or implies specific entities (companies, people, products,
  regulatory bodies, facilities) and the answer is a specific fact or relationship.
  Use for: direct lookups, single facts, multi-hop chains, disambiguation, uncertain/
  speculative questions, cross-document lookups, and questions about named regulatory
  or industry bodies.
  Examples:
  - "Who is the CEO of Apex Racquets?"
  - "Which suppliers does Apex share with Volenti?"
  - "Tell me about the company called Apex."
  - "Will Volenti renew Apex's Filamentrix contract?"
  - "How long until Apex can replace its Toray dependency?"
  - "What is the ITF's revised forward deformation range for tournament balls?"

COMBINED_COMMUNITY — The question names specific entities AND asks for thematic synthesis,
  risk aggregation, or comparison across multiple companies/domains — requiring both
  precise graph facts AND broader industry patterns.
  Also use when the question asks how broadly an industry is exposed to a named entity
  or event (e.g. a specific facility, a specific company's action).
  Examples:
  - "What are the main risks Apex Racquets faces?"
  - "What changed in Apex's business situation between Q3 2024 and Q1 2025?"
  - "How exposed is the entire premium tennis racquet industry to disruption at Toray's Mishima facility?"
  - "How does the Toray earthquake affect industry-wide supply?"

GLOBAL — The question has NO specific named entity anchor at all — it asks about abstract
  themes or patterns without naming any company, person, product, or facility.
  Only use GLOBAL when the question truly contains no named entity.
  Examples:
  - "What are the main supply chain themes across the premium tennis industry?"
  - "What are the major risks across the tennis industry?"

Question: {query}

Return ONLY a JSON object: {{"mode": "COMBINED" | "COMBINED_COMMUNITY" | "GLOBAL", "reasoning": "one sentence"}}"""


def route_query(query: str) -> Literal["combined", "combined_community", "global"]:
    """Determine the best retrieval mode for a query."""
    response = client.messages.create(
        model=ROUTING_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": ROUTING_PROMPT.format(query=query)}],
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("\n", 1)[0]
    try:
        data = json.loads(text)
        mode = data.get("mode", "COMBINED").lower()
        if mode in ("combined", "combined_community", "global"):
            return mode
    except json.JSONDecodeError:
        pass
    return "combined"  # safe default
