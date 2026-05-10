# Tennis Equipment Synthetic Corpus

A 30-document synthetic corpus designed to test Graph RAG vs vector RAG on a tennis equipment domain. Built around a fictional Austrian racquet manufacturer (Apex Racquets) and its competitors, suppliers, and athletes.

## Why this corpus

Designed specifically to expose Graph RAG's strengths:
- **Entity-rich** — manufacturers, athletes, suppliers, retailers, materials, technologies, regulatory bodies
- **Cross-document connections** — the same entities appear in 3-8 documents each, with different facts in each
- **Deliberate multi-hop facts** — six pre-planted reasoning chains where no single document contains the full answer
- **Adversarial elements** — entity disambiguation (two different Apexes, multiple Klaus Reinhardts), red herrings, contradicted facts

## Files

```
docs/
  01-10  Apex Racquets internal documents (10-K, earnings, memos, press releases, Slack)
  11-19  News and analyst coverage
  20-22  Volenti Sport (competitor) documents
  23-26  Suppliers and retail partners (Toray, Filamentrix, Hanil, TennisWarehouse)
  27-30  Adversarial / disambiguation / historical context
queries.json  Labeled test set with 20 queries across 6 categories
```

## Multi-hop chains deliberately planted

Each chain spans documents that don't all mention each other directly:

| # | Chain | Span |
|---|-------|------|
| 1 | Apex → Toray Mishima → earthquake → Hanil alternative | Docs 1, 5, 11, 18, 25 |
| 2 | Apex → Filamentrix supplier → acquired by Volenti → competitor risk | Docs 1, 12, 21, 22 |
| 3 | Apex's stringer Klimt → also strings for Drako → Volenti's lead player | Docs 6, 9, 13, 19 |
| 4 | Petrović → previously Volenti → contract dispute → switched to Apex | Docs 6, 13, 14 |
| 5 | TennisWarehouse → promotes Volenti Strike Pro → undercuts Apex pricing | Docs 1, 17, 22, 26 |
| 6 | ITF ball spec change → ApexCourt ball line → revenue impact | Docs 1, 2, 16 |

## Cast of characters

**Apex Racquets (the focal company)**
- Klaus Reinhardt — CEO, formerly at Volenti
- Anya Kostova — CFO
- Henrik Larsson — CTO, R&D lead
- Sofia Brennan, Tomáš Novak — engineering team

**Athletes**
- Marko Petrović — Croatian, Apex's lead endorser, currently injured
- Lucia Marchetti — Italian WTA, Apex secondary endorser
- Aleš Drako — Slovenian, Volenti's lead endorser
- Bruno Klimt — pro stringer (works for both Petrović AND Drako — multi-hop seed)

**Competitors**
- Volenti Sport (Italy) — primary competitor, larger market share
- Crestwood Athletic (US)
- Yokota Tennis (Japan)

**Suppliers**
- Toray Carbon (Japan, Mishima facility) — primary carbon fiber supplier industry-wide
- Filamentrix (Italy) — Apex's primary string supplier, acquired by Volenti
- Hanil Composites (South Korea) — alternative carbon fiber supplier in qualification

**Retail**
- TennisWarehouse — dominant North American specialty retailer

## How to use this corpus

### 1. Drop into your Graph RAG POC

```python
import os
from pathlib import Path

corpus_dir = Path("tennis_corpus/docs")
documents = []
for path in sorted(corpus_dir.glob("*.md")):
    with open(path) as f:
        documents.append({"id": path.stem, "content": f.read()})
```

Run your extraction → graph build → community detection pipeline.

### 2. Build a parallel vector RAG baseline

Same documents, same chunking, embed and index in numpy. This is your control group.

### 3. Run both systems against `queries.json`

For each query:
- Run vector RAG → get answer A
- Run Graph RAG → get answer B
- Score both against `expected_facts` (manual or LLM-as-judge)

### 4. Tabulate by category

| Category | Vector RAG accuracy | Graph RAG accuracy |
|----------|--------------------|--------------------| 
| Single-fact (Q1-Q3, Q14, Q19) | should be ~tied | should be ~tied |
| Two-hop (Q4-Q5, Q7-Q9, Q13, Q17, Q20) | should drop noticeably | should hold up |
| Three-hop (Q6) | should fail | should succeed |
| Thematic (Q10-Q12, Q18) | should produce generic answers | should produce specific cross-document answers |
| Adversarial (Q15) | may conflate entities | should disambiguate |
| Uncertain (Q16) | risk of fabrication | should say unresolved |

If Graph RAG isn't winning the multi-hop and thematic categories meaningfully, your extraction or retrieval logic has a bug — the corpus is designed so it should.

## Quantitative evaluation suggestions

For LLM-as-judge scoring with Claude:

```
Given the expected facts: [from queries.json]
And the system's answer: [from your pipeline]

Score on 0-3:
  0 = wrong or missing the answer entirely
  1 = partial — gets some facts but misses key points or includes errors
  2 = mostly correct — gets the substantive answer but missing nuance
  3 = fully correct — captures all expected facts accurately
```

Run each query 3 times to account for non-determinism, average the scores.

## Limitations of this corpus

- **Synthetic** — clean, well-structured prose. Real-world corpora have OCR errors, inconsistent formatting, etc.
- **Small scale** — 30 docs. Doesn't stress-test indexing pipelines or community detection at scale.
- **Single domain** — won't reveal cross-domain entity resolution issues.

After validating on this corpus, move to a real-world test (SEC filings or actual company documents) before drawing production conclusions.

## License / Attribution

This corpus is synthetic. All companies, people, products, and events are fictional. Any resemblance to real entities is coincidental.
