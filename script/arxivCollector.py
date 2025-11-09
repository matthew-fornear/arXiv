#!/usr/bin/env python3
"""
Download arXiv search results, collect metadata, and archive full papers.

The script replays the behaviour of the provided cURL requests, handles
pagination, stores metadata under `/output/<query>/metadata/`, and downloads
each paper's PDF into `/output/<query>/data/`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from tqdm import tqdm


DEFAULT_SIZE = 50
DEFAULT_ORDER = "-announced_date_first"
DEFAULT_SEARCHTYPE = "all"
DEFAULT_ABSTRACTS = "show"
DEFAULT_SOURCE = "header"
BASE_URL = "https://arxiv.org/search/"
OUTPUT_DIR = Path("/home/user/projects/arXiv/output")

RESULT_COUNT_PATTERN = re.compile(
    r"Showing\s+\d+\s*(?:&ndash;|-)\s*\d+\s+of\s+([\d,]+)\s+results",
    flags=re.IGNORECASE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect arXiv search results, metadata, and PDFs."
    )
    parser.add_argument(
        "--query",
        help="Query string to search for (required).",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=DEFAULT_SIZE,
        help="Number of results per page (default: %(default)s).",
    )
    parser.add_argument(
        "--order",
        default=DEFAULT_ORDER,
        help="Sort order (default: %(default)s).",
    )
    parser.add_argument(
        "--searchtype",
        default=DEFAULT_SEARCHTYPE,
        help="arXiv search type (default: %(default)s).",
    )
    parser.add_argument(
        "--abstracts",
        default=DEFAULT_ABSTRACTS,
        choices=("show", "hide"),
        help="Whether to include abstracts (default: %(default)s).",
    )
    parser.add_argument(
        "--source",
        default=DEFAULT_SOURCE,
        help="Value for the 'source' query parameter (default: %(default)s).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        help="Optional cap on number of pages to fetch.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Base directory for the output tree (default: /output).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch a single page and print statistics without writing output.",
    )
    return parser.parse_args()


def build_session() -> Tuple[requests.Session, Optional[str], Optional[str]]:
    """Configure a requests Session with headers and cookie string."""
    cookie_string = os.getenv("ARXIV_COOKIE")
    if not cookie_string:
        raise RuntimeError(
            "Missing ARXIV_COOKIE environment variable. "
            "Fill it in .env using the provided cookie string."
        )

    session = requests.Session()

    headers = {
        "Accept": os.getenv(
            "ARXIV_ACCEPT",
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8",
        ),
        "Accept-Language": os.getenv("ARXIV_ACCEPT_LANGUAGE", "en-US,en;q=0.9"),
        "Cache-Control": os.getenv("ARXIV_CACHE_CONTROL", "max-age=0"),
        "Priority": os.getenv("ARXIV_PRIORITY", "u=0, i"),
        "Sec-CH-UA": os.getenv(
            "ARXIV_SEC_CH_UA",
            '"Chromium";v="142", "Brave";v="142", "Not_A Brand";v="99"',
        ),
        "Sec-CH-UA-Mobile": os.getenv("ARXIV_SEC_CH_UA_MOBILE", "?0"),
        "Sec-CH-UA-Platform": os.getenv("ARXIV_SEC_CH_UA_PLATFORM", '"Linux"'),
        "Sec-Fetch-Dest": os.getenv("ARXIV_SEC_FETCH_DEST", "document"),
        "Sec-Fetch-Mode": os.getenv("ARXIV_SEC_FETCH_MODE", "navigate"),
        "Sec-Fetch-User": os.getenv("ARXIV_SEC_FETCH_USER", "?1"),
        "Sec-GPC": os.getenv("ARXIV_SEC_GPC", "1"),
        "Upgrade-Insecure-Requests": os.getenv(
            "ARXIV_UPGRADE_INSECURE_REQUESTS", "1"
        ),
        "User-Agent": os.getenv(
            "ARXIV_USER_AGENT",
            (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
            ),
        ),
    }

    session.headers.update(headers)
    session.headers["Cookie"] = cookie_string  # Preserve raw formatting.
    initial_referer = os.getenv("ARXIV_REFERER")
    initial_sec_fetch_site = os.getenv("ARXIV_SEC_FETCH_SITE", "cross-site")
    return session, initial_referer, initial_sec_fetch_site


def extract_total_results(soup: BeautifulSoup) -> Optional[int]:
    """Extract the total number of results from the result summary text."""
    counters = soup.select("p.title.is-clearfix, p")
    for element in counters:
        text = element.get_text(strip=True)
        if "Showing" in text and "results" in text:
            match = RESULT_COUNT_PATTERN.search(text)
            if match:
                return int(match.group(1).replace(",", ""))
    return None


def extract_records(soup: BeautifulSoup) -> List[Dict[str, str]]:
    """Collect metadata records from a result page."""
    records: List[Dict[str, str]] = []
    for item in soup.select("li.arxiv-result"):
        title_el = item.select_one("p.title.is-5")
        title = (
            " ".join(title_el.stripped_strings) if title_el is not None else ""
        )

        abs_el = item.select_one('p.list-title a[href^="https://arxiv.org/abs/"]')
        if abs_el is None:
            continue
        abs_url = abs_el.get("href", "").strip()
        identifier = abs_el.get_text(strip=True).replace("arXiv:", "").strip()
        if not identifier and abs_url:
            identifier = abs_url.rstrip("/").split("/")[-1]

        pdf_el = item.select_one('p.list-title a[href^="https://arxiv.org/pdf/"]')
        pdf_url = None
        if pdf_el is not None:
            pdf_url = pdf_el.get("href", "").strip()
            if pdf_url and not pdf_url.endswith(".pdf"):
                pdf_url = pdf_url.split("?", 1)[0]

        record: Dict[str, str] = {
            "id": identifier,
            "title": title,
            "abs_url": abs_url,
        }
        if pdf_url:
            record["pdf_url"] = pdf_url
        records.append(record)
    return records


def fetch_page(
    session: requests.Session,
    params: Dict[str, str],
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[BeautifulSoup, str]:
    response = session.get(BASE_URL, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser"), response.url


def collect_records(
    session: requests.Session,
    query: str,
    size: int,
    order: str,
    searchtype: str,
    abstracts: str,
    max_pages: Optional[int] = None,
    initial_referer: Optional[str] = None,
    initial_sec_fetch_site: Optional[str] = None,
    source: str = DEFAULT_SOURCE,
) -> List[Dict[str, str]]:
    collected: List[Dict[str, str]] = []
    seen: set[str] = set()
    start = 0
    total_results: Optional[int] = None
    page_index = 0

    last_referer: Optional[str] = initial_referer
    progress = tqdm(desc="Collecting records", unit="record", total=None)

    while True:
        if max_pages is not None and page_index >= max_pages:
            break

        params = {
            "query": query,
            "searchtype": searchtype,
            "abstracts": abstracts,
            "order": order,
            "size": str(size),
            "source": source,
        }

        if start > 0:
            params["start"] = str(start)

        page_headers: Dict[str, str] = {}
        if last_referer:
            page_headers["Referer"] = last_referer
        elif initial_referer:
            page_headers["Referer"] = initial_referer
        if initial_sec_fetch_site:
            page_headers["Sec-Fetch-Site"] = (
                "same-origin" if last_referer else initial_sec_fetch_site
            )

        soup, fetched_url = fetch_page(
            session, params, headers=page_headers or None
        )
        last_referer = fetched_url
        page_records = extract_records(soup)
        if not page_records:
            break
        for record in page_records:
            record_id = record.get("id")
            if not record_id:
                continue
            if record_id in seen:
                continue
            seen.add(record_id)
            collected.append(record)
            progress.update(1)

        if total_results is None:
            total_results = extract_total_results(soup)
            if total_results:
                progress.total = total_results
                progress.refresh()

        start += size
        page_index += 1

        if total_results is not None and start >= total_results:
            break

    progress.close()
    return collected


def slugify(value: str) -> str:
    safe = re.sub(r"\s+", "_", value.strip())
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", safe)
    safe = safe.strip("_.")
    return safe or "search"


def sanitize_filename(value: str) -> str:
    return slugify(value)


def download_papers(
    session: requests.Session, records: List[Dict[str, str]], data_dir: Path
) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for record in tqdm(records, desc="Downloading PDFs", unit="record"):
        pdf_url = record.get("pdf_url")
        identifier = record.get("id") or ""
        if not pdf_url or not identifier:
            continue
        filename = sanitize_filename(identifier) + ".pdf"
        destination = data_dir / filename
        if destination.exists():
            record["pdf_local_path"] = str(destination)
            continue
        try:
            response = session.get(
                pdf_url,
                headers={
                    "Referer": record.get("abs_url", BASE_URL),
                    "Sec-Fetch-Site": "same-origin",
                },
                timeout=120,
            )
            response.raise_for_status()
            destination.write_bytes(response.content)
            record["pdf_local_path"] = str(destination)
        except Exception as exc:  # pylint: disable=broad-except
            record["pdf_download_error"] = str(exc)


def write_output(
    session: requests.Session,
    records: List[Dict[str, str]],
    output_root: Path,
    query: str,
    size: int,
    order: str,
    searchtype: str,
    abstracts: str,
    source: str,
) -> Path:
    query_slug = slugify(query)
    base_dir = output_root / query_slug
    metadata_dir = base_dir / "metadata"
    data_dir = base_dir / "data"
    metadata_dir.mkdir(parents=True, exist_ok=True)

    download_papers(session, records, data_dir)

    metadata_file = metadata_dir / "results.json"
    payload = {
        "metadata": {
            "query": query,
            "size": size,
            "order": order,
            "searchtype": searchtype,
            "abstracts": abstracts,
            "source": source,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "count": len(records),
        },
        "records": records,
    }
    with metadata_file.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")

    return base_dir


def main() -> None:
    load_dotenv()
    args = parse_args()

    if not args.query:
        print("Please supply args in the form of --query <your search> to use the script.")
        return

    output_root = args.output if args.output else OUTPUT_DIR
    output_root.mkdir(parents=True, exist_ok=True)

    session, initial_referer, initial_sec_fetch_site = build_session()
    records = collect_records(
        session=session,
        query=args.query,
        size=args.size,
        order=args.order,
        searchtype=args.searchtype,
        abstracts=args.abstracts,
        max_pages=args.max_pages,
        initial_referer=initial_referer,
        initial_sec_fetch_site=initial_sec_fetch_site,
        source=args.source,
    )

    if args.dry_run:
        print(f"Collected {len(records)} records (dry-run, no files written).")
        for record in records[:5]:
            print(f"{record.get('id')}: {record.get('title')}")
        return

    base_dir = write_output(
        session=session,
        records=records,
        output_root=output_root,
        query=args.query,
        size=args.size,
        order=args.order,
        searchtype=args.searchtype,
        abstracts=args.abstracts,
        source=args.source,
    )
    print(f"Archived {len(records)} records under {base_dir}")


if __name__ == "__main__":
    main()

