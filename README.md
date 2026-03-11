# paper-fetcher

A generic academic paper fetcher for weekly curation workflows. Fetches new papers from multiple sources, deduplicates them, and saves structured JSON for downstream filtering.

## Sources

| Source | Coverage | Abstracts | Blocking risk | Primary use |
|--------|----------|-----------|---------------|-------------|
| arXiv API | All physics/CS preprints | 100% | None | Primary: physics journals |
| Nature/IOP RSS | npj CM, NatCompSci, MLST | ~60% | None | Primary: those journals |
| CrossRef | APS journals (via ISSN) | ~15% | None | Fallback for blocked journals |
| scholarly | Any journal, topic-based | ~80% | Minimal (weekly) | Optional: topic sweep |
| tracked authors | Semantic Scholar + arXiv | ~90% | None | Optional: follow specific researchers |

APS RSS feeds are Cloudflare-blocked at the IP level — CrossRef is used as a fallback for Physical Review B, Physical Review Materials, and PRX Intelligence.

## Installation

```bash
cd paper-fetcher
uv venv .venv
uv pip install feedparser requests          # core dependencies
uv pip install scholarly                    # optional: only for --scholarly flag
```

## Configuration

### `config.local.json` (private — never committed)

Create this file in your working directory (not in this repo):

```json
{
  "unpaywall_email": "YOUR_EMAIL@example.com",
  "base_dir": "/path/to/your/paper_curation"
}
```

- `unpaywall_email`: Used for the Unpaywall OA PDF lookup API and CrossRef polite pool. Required for PDF downloads.
- `base_dir`: Root directory where `data/`, `curations/`, and `pdfs/` subdirectories will be created.

### Profile JSON

A profile defines which journals and arXiv categories to fetch, and the curation criteria. See `profiles/example-condensed-matter.json` for a complete example.

Key fields:

```json
{
  "name": "my_profile",
  "display_name": "My Topic Area",
  "lookback_days": 7,
  "journals": [
    {"name": "Journal Name", "url": "https://rss-feed-url", "oa": true},
    {"name": "APS Journal",  "crossref_issn": "XXXX-XXXX",  "oa": false}
  ],
  "arxiv_categories": [
    {"id": "cond-mat.str-el", "name": "Strongly Correlated Electrons"}
  ],
  "scholarly_queries": [
    "machine learning topological insulator"
  ],
  "curation_topics": {
    "keep": ["...topics to keep..."],
    "discard": ["...topics to discard..."],
    "star_3": "Top-priority criteria",
    "star_2": "Mid-priority criteria",
    "star_1": "Low-priority criteria",
    "include_threshold": "star_2",
    "max_papers": null
  }
}
```

- Journals with `"url"` → fetched via RSS
- Journals with `"crossref_issn"` → fetched via CrossRef API
- `arxiv_categories` → fetched via arXiv API (always full abstracts)

### Tracked authors

To follow specific researchers (like Google Scholar "Follow"), add a `tracked_authors` array to your profile:

```json
"tracked_authors": [
  {
    "name": "Firstname Lastname",
    "semanticscholar_id": "12345678",
    "arxiv_name": "Lastname_F"
  }
]
```

- `semanticscholar_id`: Find once via `https://api.semanticscholar.org/graph/v1/author/search?query=Name&fields=name,authorId,paperCount,hIndex`
- `arxiv_name`: Used for supplemental arXiv author search (`au:Lastname_F`) to catch preprints not yet in Semantic Scholar
- Papers from tracked authors **bypass keyword filtering** — they always appear in the output
- Enable with `--tracked-authors` flag (off by default to avoid unnecessary API requests)

### Two-group keyword filter

For broad/noisy arXiv categories (e.g., cs.LG with 500+ papers/week), add `"filtered": true` to the category and define `keyword_filter.keywords` in the profile. Papers from filtered categories that don't contain any keyword in their title are discarded and added to `seen` (won't re-appear):

```json
"arxiv_categories": [
  {"id": "cond-mat.str-el", "name": "Strongly Correlated Electrons"},
  {"id": "cs.LG", "name": "Machine Learning", "filtered": true}
],
"keyword_filter": {
  "keywords": ["magnetic", "topological", "DFT", "correlated", "spin", "phonon"]
}
```

Journals (RSS + CrossRef) and physics arXiv categories are **never filtered** — only categories with `"filtered": true`.

## Usage

```bash
# Core fetch (arXiv + RSS + CrossRef)
.venv/bin/python fetch_papers.py \
  --profile /path/to/profiles/my-profile.json \
  --config /path/to/config.local.json

# With optional Google Scholar sweep
.venv/bin/python fetch_papers.py \
  --profile /path/to/profiles/my-profile.json \
  --config /path/to/config.local.json \
  --scholarly

# With tracked-author fetch (Semantic Scholar + arXiv author search)
.venv/bin/python fetch_papers.py \
  --profile /path/to/profiles/my-profile.json \
  --config /path/to/config.local.json \
  --tracked-authors

# Override lookback window
.venv/bin/python fetch_papers.py \
  --profile /path/to/profiles/my-profile.json \
  --config /path/to/config.local.json \
  --days 14

# Skip Semantic Scholar abstract enrichment (faster)
.venv/bin/python fetch_papers.py \
  --profile /path/to/profiles/my-profile.json \
  --config /path/to/config.local.json \
  --no-enrich
```

Output: `{base_dir}/data/{profile_name}/papers_YYYY-MM-DD.json`

## Download PDFs

```bash
.venv/bin/python download_pdf.py "<doi>" "<title>" \
  --profile-name my_profile \
  --config /path/to/config.local.json

# If arXiv ID is already known (faster, skips S2 lookup)
.venv/bin/python download_pdf.py "<doi>" "<title>" \
  --arxiv-id 2504.16418 \
  --profile-name my_profile \
  --config /path/to/config.local.json
```

PDF download priority:
1. Unpaywall (requires email in config)
2. arXiv direct (if `--arxiv-id` provided)
3. Nature direct PDF pattern (for 10.1038/... DOIs)
4. Semantic Scholar → arXiv fallback

Output: `{base_dir}/pdfs/{profile_name}/{sanitized_title}.pdf`

## Output format

Each entry in `papers_YYYY-MM-DD.json`:

```json
{
  "key": "arxiv:2504.16418",
  "arxiv_id": "2504.16418",
  "doi": "10.1103/PhysRevB.112.045123",
  "title": "Paper title",
  "abstract": "Full abstract text...",
  "abstract_source": "arxiv",
  "url": "https://arxiv.org/abs/2504.16418",
  "pdf_url": "https://arxiv.org/pdf/2504.16418",
  "journal": "arXiv preprint",
  "date": "2025-04-28",
  "authors": ["Author One", "Author Two"],
  "is_oa": true,
  "source": "arxiv",
  "arxiv_categories": ["cond-mat.str-el", "cond-mat.mes-hall"],
  "primary_category": "cond-mat.str-el"
}
```

The `source` field is one of: `"arxiv"`, `"rss"`, `"crossref"`, `"scholarly"`, `"tracked_author"`.

Papers from tracked authors also include `"tracked_author_name": "Firstname Lastname"`.
