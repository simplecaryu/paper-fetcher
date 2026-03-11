#!/usr/bin/env python3
"""
download_pdf.py — Download OA PDFs via Unpaywall, arXiv fallback, and Nature direct URL.

Usage:
  python download_pdf.py "<doi>" "<title>" --profile-name condensed_matter --config config.local.json
  python download_pdf.py "<doi>" "<title>" --arxiv-id 2504.16418 --profile-name condensed_matter --config config.local.json
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests


def load_json(path):
    with open(path) as f:
        return json.load(f)


def sanitize_filename(title, max_length=80):
    """Convert title to a safe filename."""
    name = re.sub(r'[^\w\s-]', '', title)
    name = re.sub(r'\s+', '_', name.strip())
    return name[:max_length]


def query_unpaywall(doi, email):
    """
    Query Unpaywall for OA PDF URL.
    Returns (pdf_url, landing_url) — either may be None.
    """
    if not doi:
        return None, None
    try:
        resp = requests.get(
            f"https://api.unpaywall.org/v2/{doi}?email={email}",
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            if not data.get("is_oa"):
                return None, None
            best = data.get("best_oa_location") or {}
            pdf_url = best.get("url_for_pdf")
            landing_url = best.get("url_for_landing_page")
            if not pdf_url:
                for loc in data.get("oa_locations", []):
                    if loc.get("url_for_pdf"):
                        pdf_url = loc["url_for_pdf"]
                        break
                    if loc.get("url") and not landing_url:
                        landing_url = loc["url"]
            return pdf_url, landing_url
    except Exception as e:
        print(f"  Unpaywall query failed: {e}", file=sys.stderr)
    return None, None


def query_semantic_scholar(doi):
    """Return ArXiv ID for a DOI via Semantic Scholar, or None."""
    try:
        r = requests.get(
            f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
            params={"fields": "externalIds"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("externalIds", {}).get("ArXiv")
    except Exception:
        pass
    return None


def download_file(url, dest_path):
    """Download file from URL to dest_path. Returns True on success."""
    headers = {
        "User-Agent": "paper-fetcher/1.0 (see README)",
        "Accept": "application/pdf,*/*",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=60)
        if resp.status_code != 200:
            print(f"  HTTP {resp.status_code} from {url}")
            return False
        content = resp.content
        if content[:4] == b"%PDF":
            with open(dest_path, "wb") as f:
                f.write(content)
            return True
        ctype = resp.headers.get("Content-Type", "")
        print(f"  Not a PDF (Content-Type: {ctype}, first bytes: {content[:20]})")
    except Exception as e:
        print(f"  Download error: {e}", file=sys.stderr)
    return False


def try_nature_direct_pdf(doi):
    """
    Nature OA papers sometimes serve PDFs at article_url_reference.pdf.
    Construct the URL from the DOI suffix.
    """
    m = re.match(r"10\.\d+/(.+)", doi)
    if not m:
        return None
    article_id = m.group(1)
    return f"https://www.nature.com/articles/{article_id}_reference.pdf"


def main():
    parser = argparse.ArgumentParser(description="Download OA PDF for a paper by DOI.")
    parser.add_argument("doi", help="DOI of the paper")
    parser.add_argument("title", help="Paper title (used for filename)")
    parser.add_argument("--arxiv-id", default=None, help="ArXiv ID if known (skips S2 lookup)")
    parser.add_argument("--config", required=True, help="Path to config.local.json (email, base_dir)")
    parser.add_argument("--profile-name", required=True, help="Profile name (used to build output path)")
    args = parser.parse_args()

    config = load_json(args.config)
    base_dir = Path(config["base_dir"])
    email = config.get("unpaywall_email", "")

    pdfs_dir = base_dir / "pdfs" / args.profile_name
    pdfs_dir.mkdir(parents=True, exist_ok=True)

    filename = sanitize_filename(args.title) + ".pdf"
    dest_path = pdfs_dir / filename

    if dest_path.exists():
        print(f"Already downloaded: {dest_path}")
        return

    print(f"Looking up PDF for DOI: {args.doi}")
    pdf_url = None

    # ── Step 1: Unpaywall ──────────────────────────────────────────────────
    if email and email not in ("YOUR_EMAIL@example.com", ""):
        uw_pdf, uw_landing = query_unpaywall(args.doi, email)
        if uw_pdf:
            print(f"  Unpaywall: found direct PDF → {uw_pdf}")
            pdf_url = uw_pdf
        elif uw_landing:
            print(f"  Unpaywall: OA but no direct PDF URL (landing: {uw_landing})")
        else:
            print(f"  Unpaywall: not found or not OA")

    # ── Step 2: arXiv direct URL (if arxiv_id already known) ──────────────
    if not pdf_url and args.arxiv_id:
        pdf_url = f"https://arxiv.org/pdf/{args.arxiv_id}"
        print(f"  Using provided arXiv ID: {pdf_url}")

    # ── Step 3: Nature direct PDF pattern ─────────────────────────────────
    if not pdf_url and "10.1038" in args.doi:
        candidate = try_nature_direct_pdf(args.doi)
        if candidate:
            print(f"  Trying Nature direct PDF: {candidate}")
            pdf_url = candidate

    # ── Step 4: Semantic Scholar → arXiv fallback ─────────────────────────
    if not pdf_url:
        print("  Querying Semantic Scholar for ArXiv ID...")
        arxiv_id = query_semantic_scholar(args.doi)
        if arxiv_id:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
            print(f"  ArXiv fallback (id={arxiv_id}): {pdf_url}")
        else:
            print("  No ArXiv preprint found")

    if not pdf_url:
        print(f"PDF not available for: {args.title}")
        sys.exit(1)

    # ── Download ───────────────────────────────────────────────────────────
    print(f"Downloading → {dest_path}")
    if download_file(pdf_url, dest_path):
        size_kb = os.path.getsize(dest_path) // 1024
        print(f"Downloaded ({size_kb} KB): {dest_path}")
    else:
        # If Nature _reference.pdf failed, try arXiv as a second chance
        if "nature.com" in pdf_url and "_reference.pdf" in pdf_url:
            print("  Nature PDF failed, trying ArXiv...")
            arxiv_id = args.arxiv_id or query_semantic_scholar(args.doi)
            if arxiv_id:
                pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
                print(f"  Retrying with arXiv: {pdf_url}")
                if download_file(pdf_url, dest_path):
                    size_kb = os.path.getsize(dest_path) // 1024
                    print(f"Downloaded ({size_kb} KB): {dest_path}")
                    return
        print(f"PDF not available for: {args.title}")
        sys.exit(1)


if __name__ == "__main__":
    main()
