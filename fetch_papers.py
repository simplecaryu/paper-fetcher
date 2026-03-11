#!/usr/bin/env python3
"""
fetch_papers.py — Generic academic paper fetcher.

Sources (in priority order):
  1. arXiv API      — physics preprints; full abstracts always present
  2. RSS feeds      — Nature/IOP journals with working feeds
  3. CrossRef API   — APS journals (Cloudflare-blocked RSS); metadata only (~15% abstracts)
  4. scholarly      — optional topic sweep via Google Scholar (--scholarly flag)

Usage:
  python fetch_papers.py --profile profiles/my-profile.json --config config.local.json
  python fetch_papers.py --profile profiles/my-profile.json --config config.local.json --scholarly
"""

import argparse
import feedparser
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


# ---------------------------------------------------------------------------
# Config / profile loading
# ---------------------------------------------------------------------------

def load_json(path):
    with open(path) as f:
        return json.load(f)


def load_seen(seen_path):
    if os.path.exists(seen_path):
        with open(seen_path) as f:
            return json.load(f)
    return {}


def save_seen(seen_path, seen):
    Path(seen_path).parent.mkdir(parents=True, exist_ok=True)
    with open(seen_path, "w") as f:
        json.dump(seen, f, indent=2)


# ---------------------------------------------------------------------------
# Key / DOI helpers
# ---------------------------------------------------------------------------

def paper_key(entry):
    """Stable dedup key: prefer DOI, fall back to URL hash."""
    doi = extract_doi(entry)
    if doi:
        return f"doi:{doi}"
    return f"url:{hashlib.md5(entry.get('link', '').encode()).hexdigest()}"


def extract_doi(entry):
    """Try to extract DOI from various feed fields."""
    doi = entry.get("prism_doi") or entry.get("dc_identifier", "")
    if doi and doi.startswith("10."):
        return doi
    for field in [entry.get("id", ""), entry.get("link", "")]:
        m = re.search(r"10\.\d{4,}/\S+", field)
        if m:
            return m.group(0).rstrip(".,)")
    return None


def extract_abstract(entry):
    """
    Extract abstract text from RSS entry.

    Feed-specific behaviour:
    - Nature (npj CM, NatCompSci): content/summary = "<p>Journal, date; doi-link</p>Abstract text"
      The abstract lives after the closing </p> tag. Some papers omit it entirely.
    - IOP (MLST): full abstract is in the `summary` field as plain text.

    Returns (abstract_text, is_full) where is_full=False means stub/title repeat only.
    """
    raw = ""
    content = entry.get("content")
    if content and isinstance(content, list) and content[0].get("value"):
        raw = content[0]["value"]
    elif entry.get("summary"):
        raw = entry["summary"]

    if not raw:
        return "", False

    # Nature pattern: strip the leading <p>...</p> boilerplate, keep what follows
    after_p = re.split(r"</p>", raw, maxsplit=1)
    if len(after_p) == 2:
        candidate = re.sub(r"<[^>]+>", " ", after_p[1])
        candidate = re.sub(r"\s+", " ", candidate).strip()
        title = entry.get("title", "").strip()
        if candidate and candidate.lower() != title.lower():
            return candidate, True
        return "", False

    # Plain text (IOP): just strip any residual HTML
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    title = entry.get("title", "").strip()
    if text and text.lower() != title.lower():
        return text, True
    return "", False


def extract_authors(entry):
    if entry.get("authors"):
        return [a.get("name", "") for a in entry["authors"]]
    elif entry.get("author"):
        return [entry["author"]]
    return []


def parse_date(entry):
    """Return ISO date string from entry, or None."""
    for field in ["published_parsed", "updated_parsed"]:
        t = entry.get(field)
        if t:
            try:
                dt = datetime(*t[:6], tzinfo=timezone.utc)
                return dt.strftime("%Y-%m-%d")
            except Exception:
                pass
    return None


def is_within_lookback(entry, cutoff_date):
    for field in ["published_parsed", "updated_parsed"]:
        t = entry.get(field)
        if t:
            try:
                dt = datetime(*t[:6], tzinfo=timezone.utc)
                return dt >= cutoff_date
            except Exception:
                pass
    return True  # include if no date


# ---------------------------------------------------------------------------
# Source 1: arXiv API
# ---------------------------------------------------------------------------

def fetch_arxiv(profile, cutoff_date, seen):
    """
    Fetch recent submissions from arXiv for the categories in the profile.
    Returns list of paper dicts. Full abstracts always present.
    """
    categories = profile.get("arxiv_categories", [])
    if not categories:
        return []

    cats_query = "+OR+".join(f"cat:{c['id']}" for c in categories)
    from_date = cutoff_date.strftime("%Y%m%d0000")
    to_date = datetime.now(timezone.utc).strftime("%Y%m%d2359")
    url = (
        f"https://export.arxiv.org/api/query"
        f"?search_query=({cats_query})+AND+submittedDate:[{from_date}+TO+{to_date}]"
        f"&start=0&max_results=2000&sortBy=submittedDate&sortOrder=descending"
    )

    try:
        feed = feedparser.parse(url)
    except Exception as e:
        print(f"  WARNING: arXiv API fetch failed: {e}", file=sys.stderr)
        return []

    if not feed.entries:
        status = getattr(feed, "status", "?")
        if status and status != 200:
            print(f"  WARNING: arXiv API returned status {status}", file=sys.stderr)
        return []

    papers = []
    for entry in feed.entries:
        # arXiv ID: strip version suffix e.g. "2504.16418v1" → "2504.16418"
        raw_id = entry.id.split("/abs/")[-1]
        arxiv_id = re.sub(r"v\d+$", "", raw_id)
        key = f"arxiv:{arxiv_id}"
        if key in seen:
            continue

        doi = entry.get("arxiv_doi") or None
        # Also check doi: key to avoid duplicating CrossRef/RSS entries
        doi_key = f"doi:{doi}" if doi else None

        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        authors = [a.name for a in getattr(entry, "authors", [])]
        abstract = entry.get("summary", "")
        abstract = re.sub(r"\s+", " ", abstract).strip()

        # Primary category (first tag)
        tags = getattr(entry, "tags", [])
        primary_cat = tags[0].term if tags else ""
        all_cats = [t.term for t in tags]

        papers.append({
            "key": key,
            "arxiv_id": arxiv_id,
            "doi": doi,
            "_doi_key": doi_key,   # used for cross-source dedup; stripped before output
            "title": entry.get("title", "").strip().replace("\n", " "),
            "abstract": abstract,
            "abstract_source": "arxiv",
            "url": f"https://arxiv.org/abs/{arxiv_id}",
            "pdf_url": pdf_url,
            "journal": entry.get("arxiv_journal_ref") or "arXiv preprint",
            "date": parse_date(entry),
            "authors": authors,
            "is_oa": True,
            "arxiv_categories": all_cats,
            "primary_category": primary_cat,
            "source": "arxiv",
        })

    return papers


# ---------------------------------------------------------------------------
# Source 2: RSS feeds (Nature, IOP)
# ---------------------------------------------------------------------------

def fetch_journal_rss(journal, cutoff_date, seen):
    """Fetch one journal's RSS feed. Returns list of paper dicts."""
    name = journal["name"]
    url = journal["url"]
    is_oa = journal.get("oa", False)

    try:
        feed = feedparser.parse(url)
    except Exception as e:
        print(f"  WARNING: Failed to fetch {name}: {e}", file=sys.stderr)
        return []

    if feed.bozo and not feed.entries:
        status = getattr(feed, "status", "?")
        if status == 403:
            print(f"  WARNING: {name} RSS blocked (HTTP 403)", file=sys.stderr)
        else:
            print(f"  WARNING: Could not parse feed for {name} (status={status})", file=sys.stderr)
        return []

    papers = []
    for entry in feed.entries:
        if not is_within_lookback(entry, cutoff_date):
            continue

        key = paper_key(entry)
        if key in seen:
            continue

        doi = extract_doi(entry)
        abstract, abstract_full = extract_abstract(entry)

        paper = {
            "key": key,
            "title": entry.get("title", "").strip(),
            "abstract": abstract,
            "abstract_source": "rss" if abstract_full else "",
            "url": entry.get("link", ""),
            "doi": doi,
            "_doi_key": f"doi:{doi}" if doi else None,
            "journal": name,
            "date": parse_date(entry),
            "authors": extract_authors(entry),
            "is_oa": is_oa,
            "source": "rss",
        }
        papers.append(paper)

    return papers


# ---------------------------------------------------------------------------
# Source 3: CrossRef fallback (APS journals)
# ---------------------------------------------------------------------------

def fetch_via_crossref(journal_name, issn, cutoff_date, seen, is_oa, email):
    """
    Query CrossRef for recent articles from a journal by ISSN.
    Returns list of paper dicts.
    """
    polite = f"&mailto={email}" if email else ""
    url = (
        f"https://api.crossref.org/works"
        f"?filter=issn:{issn},from-pub-date:{cutoff_date.strftime('%Y-%m-%d')}"
        f"&sort=published&order=desc&rows=50{polite}"
    )
    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200:
            print(f"  WARNING: CrossRef returned {resp.status_code} for {journal_name}", file=sys.stderr)
            return []
        items = resp.json().get("message", {}).get("items", [])
    except Exception as e:
        print(f"  WARNING: CrossRef fetch failed for {journal_name}: {e}", file=sys.stderr)
        return []

    papers = []
    for item in items:
        doi = item.get("DOI", "")
        if not doi:
            continue
        key = f"doi:{doi}"
        if key in seen:
            continue

        date_str = None
        for date_field in ["published-online", "published-print", "created"]:
            dp = item.get(date_field, {}).get("date-parts", [[]])
            if dp and dp[0]:
                parts = dp[0]
                try:
                    date_str = datetime(*parts + [1] * (3 - len(parts))).strftime("%Y-%m-%d")
                    break
                except Exception:
                    pass

        title_list = item.get("title", [])
        title = title_list[0] if title_list else ""

        authors = []
        for a in item.get("author", []):
            name = " ".join(filter(None, [a.get("given", ""), a.get("family", "")]))
            if name:
                authors.append(name)

        abstract = item.get("abstract", "")
        if abstract:
            abstract = re.sub(r"<[^>]+>", " ", abstract)
            abstract = re.sub(r"\s+", " ", abstract).strip()

        papers.append({
            "key": key,
            "title": title.strip(),
            "abstract": abstract,
            "abstract_source": "crossref" if abstract else "",
            "url": f"https://doi.org/{doi}",
            "doi": doi,
            "_doi_key": key,
            "journal": journal_name,
            "date": date_str,
            "authors": authors,
            "is_oa": is_oa,
            "source": "crossref",
        })

    return papers


# ---------------------------------------------------------------------------
# Source 4: scholarly (Google Scholar, optional)
# ---------------------------------------------------------------------------

def fetch_scholarly(profile, cutoff_year, seen):
    """
    Optional topic sweep via Google Scholar using the scholarly package.
    Returns list of paper dicts. Requires: pip install scholarly
    """
    try:
        from scholarly import scholarly as sch
    except ImportError:
        print("  WARNING: scholarly not installed. Run: uv pip install scholarly", file=sys.stderr)
        return []

    queries = profile.get("scholarly_queries", [])
    if not queries:
        return []

    all_papers = []
    seen_titles = set()  # within-scholarly dedup by title

    for query in queries:
        print(f"  scholarly: searching '{query}'...")
        try:
            results = sch.search_pubs(query)
            count = 0
            for pub in results:
                if count >= 10:
                    break
                bib = pub.get("bib", {})
                year = bib.get("pub_year")
                try:
                    if year and int(year) < cutoff_year:
                        continue
                except ValueError:
                    pass

                title = bib.get("title", "").strip()
                if not title or title.lower() in seen_titles:
                    continue
                seen_titles.add(title.lower())

                # Build a dedup key from title hash (no DOI usually available)
                key = f"scholarly:{hashlib.md5(title.lower().encode()).hexdigest()[:12]}"
                if key in seen:
                    continue

                abstract = bib.get("abstract", "")
                authors = bib.get("author", [])
                if isinstance(authors, str):
                    authors = [a.strip() for a in authors.split(" and ")]

                all_papers.append({
                    "key": key,
                    "title": title,
                    "abstract": abstract,
                    "abstract_source": "scholarly" if abstract else "",
                    "url": pub.get("pub_url", ""),
                    "doi": None,
                    "_doi_key": None,
                    "journal": bib.get("venue", ""),
                    "date": str(year) if year else None,
                    "authors": authors,
                    "is_oa": False,
                    "source": "scholarly",
                    "scholarly_query": query,
                })
                count += 1
        except Exception as e:
            print(f"  WARNING: scholarly search failed for '{query}': {e}", file=sys.stderr)
        time.sleep(1)  # polite rate limiting

    return all_papers


# ---------------------------------------------------------------------------
# Abstract enrichment via Semantic Scholar
# ---------------------------------------------------------------------------

def enrich_abstracts_via_s2(papers):
    """
    For papers missing abstracts, query Semantic Scholar.
    Mutates papers in-place. Also stores arxiv_id if found.
    """
    missing = [p for p in papers if not p.get("abstract") and p.get("doi")]
    if not missing:
        return

    print(f"  Fetching {len(missing)} missing abstracts from Semantic Scholar...")
    for paper in missing:
        doi = paper["doi"]
        try:
            r = requests.get(
                f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
                params={"fields": "abstract,externalIds"},
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                abstract = data.get("abstract") or ""
                if abstract:
                    paper["abstract"] = abstract
                    paper["abstract_source"] = "semanticscholar"
                arxiv_id = data.get("externalIds", {}).get("ArXiv")
                if arxiv_id and not paper.get("arxiv_id"):
                    paper["arxiv_id"] = arxiv_id
        except Exception:
            pass
        time.sleep(0.3)


# ---------------------------------------------------------------------------
# Cross-source DOI dedup
# ---------------------------------------------------------------------------

def dedup_by_doi(all_papers):
    """
    If an arXiv entry has a DOI matching a CrossRef/RSS entry, keep the arXiv
    version (which has a full abstract + PDF URL) and drop the duplicate.
    Returns deduplicated list.
    """
    doi_to_arxiv = {}
    for p in all_papers:
        if p.get("source") == "arxiv" and p.get("doi"):
            doi_to_arxiv[p["doi"].lower()] = p

    kept = []
    skipped_dois = set()
    for p in all_papers:
        if p.get("source") == "arxiv":
            kept.append(p)
            continue
        doi = (p.get("doi") or "").lower()
        if doi and doi in doi_to_arxiv:
            skipped_dois.add(doi)
            # arXiv version already in kept; skip this duplicate
            continue
        kept.append(p)

    if skipped_dois:
        print(f"  Cross-source dedup: removed {len(skipped_dois)} duplicates (arXiv version preferred)")

    return kept


def strip_internal_fields(papers):
    """Remove fields prefixed with _ that are only used for internal dedup."""
    for p in papers:
        p.pop("_doi_key", None)
    return papers


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fetch papers from arXiv, RSS, and CrossRef.")
    parser.add_argument("--profile", required=True, help="Path to profile JSON")
    parser.add_argument("--config", required=True, help="Path to config.local.json (email, base_dir)")
    parser.add_argument("--days", type=int, default=None, help="Lookback window override (days)")
    parser.add_argument("--scholarly", action="store_true", help="Enable Google Scholar topic sweep")
    parser.add_argument("--no-enrich", action="store_true", help="Skip Semantic Scholar abstract enrichment")
    args = parser.parse_args()

    profile = load_json(args.profile)
    config = load_json(args.config)

    profile_name = profile["name"]
    base_dir = Path(config["base_dir"])
    email = config.get("unpaywall_email", "")
    lookback_days = args.days if args.days is not None else profile.get("lookback_days", 7)

    data_dir = base_dir / "data" / profile_name
    data_dir.mkdir(parents=True, exist_ok=True)

    seen_path = data_dir / "papers_seen.json"
    seen = load_seen(seen_path)

    now = datetime.now(timezone.utc)
    cutoff_date = now - timedelta(days=lookback_days)

    all_papers = []
    source_counts = {}

    # ── Source 1: arXiv API ──────────────────────────────────────────────
    cats = profile.get("arxiv_categories", [])
    if cats:
        print(f"Fetching arXiv ({len(cats)} categories, last {lookback_days} days)...")
        papers = fetch_arxiv(profile, cutoff_date, seen)
        source_counts["arXiv"] = len(papers)
        all_papers.extend(papers)
        print(f"  arXiv: {len(papers)} new papers")

    # ── Source 2: RSS feeds ──────────────────────────────────────────────
    rss_journals = [j for j in profile.get("journals", []) if "url" in j]
    if rss_journals:
        print(f"Fetching RSS feeds ({len(rss_journals)} journals)...")
        for journal in rss_journals:
            name = journal["name"]
            papers = fetch_journal_rss(journal, cutoff_date, seen)
            source_counts[name] = len(papers)
            all_papers.extend(papers)
            print(f"  {name}: {len(papers)} new papers")

    # ── Source 3: CrossRef fallback (APS) ───────────────────────────────
    crossref_journals = [j for j in profile.get("journals", []) if "crossref_issn" in j]
    if crossref_journals:
        print(f"Fetching CrossRef ({len(crossref_journals)} journals)...")
        for journal in crossref_journals:
            name = journal["name"]
            papers = fetch_via_crossref(
                name, journal["crossref_issn"], cutoff_date, seen, journal.get("oa", False), email
            )
            source_counts[name] = len(papers)
            all_papers.extend(papers)
            print(f"  {name}: {len(papers)} new papers")

    # ── Source 4: scholarly (optional) ──────────────────────────────────
    if args.scholarly:
        print("Fetching via scholarly (Google Scholar)...")
        cutoff_year = cutoff_date.year
        papers = fetch_scholarly(profile, cutoff_year, seen)
        source_counts["scholarly"] = len(papers)
        all_papers.extend(papers)
        print(f"  scholarly: {len(papers)} new papers")

    # ── Cross-source DOI dedup ───────────────────────────────────────────
    if len(all_papers) > 0:
        all_papers = dedup_by_doi(all_papers)

    # ── Abstract enrichment ──────────────────────────────────────────────
    if not args.no_enrich:
        enrich_abstracts_via_s2(all_papers)

    n_with_abstract = sum(1 for p in all_papers if p.get("abstract"))

    # ── Mark all as seen ────────────────────────────────────────────────
    for p in all_papers:
        seen[p["key"]] = p.get("date") or now.strftime("%Y-%m-%d")
        # Also mark doi key as seen to prevent future duplicates
        doi_key = p.get("_doi_key")
        if doi_key and doi_key != p["key"]:
            seen[doi_key] = seen[p["key"]]

    save_seen(seen_path, seen)

    # ── Strip internal fields and save ──────────────────────────────────
    strip_internal_fields(all_papers)

    today = now.strftime("%Y-%m-%d")
    output_path = data_dir / f"papers_{today}.json"
    with open(output_path, "w") as f:
        json.dump(all_papers, f, indent=2, ensure_ascii=False)

    total_sources = sum(1 for v in source_counts.values() if v > 0)
    print(f"\nFetched {len(all_papers)} new papers from {total_sources} sources")
    print(f"Abstracts available: {n_with_abstract}/{len(all_papers)}")
    print(f"Saved to: {output_path}")


if __name__ == "__main__":
    main()
