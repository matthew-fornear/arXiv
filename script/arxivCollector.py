#!/usr/bin/env python3
"""
Download arXiv search results, collect metadata, and archive full papers.

The script replays the behaviour of the provided cURL requests, handles
pagination, stores metadata under `/output/<query>/metadata/`, and downloads
each paper's PDF into `/output/<query>/data/`.

Enhanced with checkpoint/resume functionality and support for collecting
all papers from arXiv with duplicate prevention.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
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
MAX_RESULTS_OFFSET = 10000  # arXiv refuses start >= 10000
ARXIV_START_DATE = datetime(1991, 4, 1, tzinfo=timezone.utc)  # arXiv started in April 1991
CHECKPOINT_SAVE_INTERVAL = 100  # Save checkpoint every N records

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
        help="Query string to search for. Not needed if --collect-all is used.",
    )
    parser.add_argument(
        "--collect-all",
        action="store_true",
        help="Collect all papers from arXiv (uses date-based iteration).",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        help="Start date for collection (YYYY-MM-DD). Defaults to arXiv start (1991-04-01) for --collect-all.",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        help="End date for collection (YYYY-MM-DD). Defaults to today for --collect-all.",
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
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Don't resume from checkpoint (start fresh).",
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


def load_checkpoint(checkpoint_file: Path) -> Optional[Dict]:
    """Load checkpoint data if it exists."""
    if checkpoint_file.exists():
        try:
            with checkpoint_file.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not load checkpoint: {e}")
    return None


def save_checkpoint(
    checkpoint_file: Path,
    current_date: Optional[str],
    page_index: int,
    start: int,
    collected_count: int,
    last_successful_date: Optional[str] = None,
) -> None:
    """Save checkpoint data to resume later."""
    checkpoint_data = {
        "current_date": current_date,
        "page_index": page_index,
        "start": start,
        "collected_count": collected_count,
        "last_successful_date": last_successful_date,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with checkpoint_file.open("w", encoding="utf-8") as f:
            json.dump(checkpoint_data, f, indent=2)
    except IOError as e:
        print(f"Warning: Could not save checkpoint: {e}")


def load_collected_ids(id_db_file: Path) -> set[str]:
    """Load previously collected paper IDs from database."""
    if id_db_file.exists():
        try:
            with id_db_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("ids", []))
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not load ID database: {e}")
    return set()


def save_collected_ids(id_db_file: Path, ids: set[str]) -> None:
    """Save collected paper IDs to database."""
    data = {
        "ids": sorted(list(ids)),
        "count": len(ids),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    try:
        # Write to temporary file first, then rename for atomic operation
        temp_file = id_db_file.with_suffix(".tmp")
        with temp_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        temp_file.replace(id_db_file)
    except IOError as e:
        print(f"Warning: Could not save ID database: {e}")


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
    collected_ids: Optional[set[str]] = None,
    checkpoint_file: Optional[Path] = None,
    checkpoint_data: Optional[Dict] = None,
) -> List[Dict[str, str]]:
    collected: List[Dict[str, str]] = []
    seen: set[str] = collected_ids if collected_ids is not None else set()
    start = checkpoint_data.get("start", 0) if checkpoint_data else 0
    total_results: Optional[int] = None
    page_index = checkpoint_data.get("page_index", 0) if checkpoint_data else 0
    records_since_checkpoint = 0

    last_referer: Optional[str] = initial_referer
    progress = tqdm(desc="Collecting records", unit="record", total=None)

    while True:
        if max_pages is not None and page_index >= max_pages:
            break
        if start >= MAX_RESULTS_OFFSET:
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

        try:
            soup, fetched_url = fetch_page(
                session, params, headers=page_headers or None
            )
            last_referer = fetched_url
            page_records = extract_records(soup)
            if not page_records:
                break
            new_records_in_page = 0
            for record in page_records:
                record_id = record.get("id")
                if not record_id:
                    continue
                if record_id in seen:
                    continue
                seen.add(record_id)
                collected.append(record)
                new_records_in_page += 1
                progress.update(1)
            
            # Save checkpoint periodically
            if checkpoint_file and new_records_in_page > 0:
                records_since_checkpoint += new_records_in_page
                if records_since_checkpoint >= CHECKPOINT_SAVE_INTERVAL:
                    save_checkpoint(
                        checkpoint_file,
                        None,  # current_date not used in single-query mode
                        page_index + 1,
                        start + size,
                        len(collected) + len(seen) - (len(collected_ids) if collected_ids else 0),
                    )
                    records_since_checkpoint = 0

        except Exception as exc:
            print(f"\nError fetching page at start={start}: {exc}")
            print("Saving checkpoint before retrying...")
            if checkpoint_file:
                save_checkpoint(
                    checkpoint_file,
                    None,
                    page_index,
                    start,
                    len(collected) + len(seen) - (len(collected_ids) if collected_ids else 0),
                )
            # Wait before retrying
            time.sleep(5)
            continue

        if total_results is None:
            total_results = extract_total_results(soup)
            if total_results:
                effective_total = min(total_results, MAX_RESULTS_OFFSET)
                progress.total = effective_total
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
            # Small delay to be respectful
            time.sleep(0.5)
        except Exception as exc:  # pylint: disable=broad-except
            record["pdf_download_error"] = str(exc)
            print(f"Error downloading {identifier}: {exc}")


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
    append: bool = False,
) -> Path:
    query_slug = slugify(query)
    base_dir = output_root / query_slug
    metadata_dir = base_dir / "metadata"
    data_dir = base_dir / "data"
    metadata_dir.mkdir(parents=True, exist_ok=True)

    download_papers(session, records, data_dir)

    metadata_file = metadata_dir / "results.json"
    
    # If appending, load existing records and merge
    if append and metadata_file.exists():
        try:
            with metadata_file.open("r", encoding="utf-8") as f:
                existing_data = json.load(f)
                existing_records = existing_data.get("records", [])
                existing_ids = {r.get("id") for r in existing_records if r.get("id")}
                
                # Only add new records
                new_records = [r for r in records if r.get("id") not in existing_ids]
                records = existing_records + new_records
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not load existing metadata: {e}")
    
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


def collect_all_papers_by_date(
    session: requests.Session,
    output_root: Path,
    start_date: datetime,
    end_date: datetime,
    size: int,
    order: str,
    searchtype: str,
    abstracts: str,
    source: str,
    initial_referer: Optional[str] = None,
    initial_sec_fetch_site: Optional[str] = None,
    collected_ids: Optional[set[str]] = None,
    checkpoint_file: Optional[Path] = None,
    checkpoint_data: Optional[Dict] = None,
    no_resume: bool = False,
    dry_run: bool = False,
) -> None:
    """Collect all papers from arXiv by iterating through date ranges."""
    all_collected: List[Dict[str, str]] = []
    base_dir = output_root / "all_papers"
    metadata_dir = base_dir / "metadata"
    data_dir = base_dir / "data"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    
    # Determine starting date
    current_date = start_date
    if checkpoint_data and not no_resume:
        last_date_str = checkpoint_data.get("last_successful_date")
        if last_date_str:
            try:
                current_date = datetime.fromisoformat(last_date_str.replace("Z", "+00:00"))
                if current_date.tzinfo is None:
                    current_date = current_date.replace(tzinfo=timezone.utc)
                print(f"Resuming from date: {current_date.date()}")
            except ValueError:
                print(f"Could not parse checkpoint date, starting from {start_date.date()}")
    
    # Iterate through dates (by month to avoid hitting offset limits)
    date_iter = current_date
    progress = tqdm(desc="Collecting all papers", unit="month")
    
    while date_iter <= end_date:
        # Create date range for the month
        month_start = date_iter.replace(day=1)
        if month_start.month == 12:
            month_end = month_start.replace(year=month_start.year + 1, month=1)
        else:
            month_end = month_start.replace(month=month_start.month + 1)
        
        # Don't exceed end_date
        if month_end > end_date:
            month_end = end_date
        
        # Format dates for query
        date_from = month_start.strftime("%Y%m%d")
        date_to = month_end.strftime("%Y%m%d")
        
        # Build query for date range
        query = f"submittedDate:[{date_from}000000+TO+{date_to}235959]"
        
        # Prepare checkpoint data for this date range
        date_checkpoint_data = None
        if checkpoint_file and month_start == current_date:
            date_checkpoint_data = checkpoint_data
        
        progress.set_description(f"Collecting {month_start.strftime('%Y-%m')}")
        
        try:
            # Collect records for this date range
            month_records = collect_records(
                session=session,
                query=query,
                size=size,
                order=order,
                searchtype=searchtype,
                abstracts=abstracts,
                initial_referer=initial_referer,
                initial_sec_fetch_site=initial_sec_fetch_site,
                source=source,
                collected_ids=collected_ids,
                checkpoint_file=checkpoint_file,
                checkpoint_data=date_checkpoint_data,
            )
            
            # Filter out duplicates
            new_records = [r for r in month_records if r.get("id") not in collected_ids]
            all_collected.extend(new_records)
            
            # Update collected IDs
            for record in new_records:
                if record.get("id"):
                    collected_ids.add(record.get("id"))
            
            # Save checkpoint and metadata after each month
            if checkpoint_file and not dry_run:
                save_checkpoint(
                    checkpoint_file,
                    month_end.isoformat(),
                    0,
                    0,
                    len(collected_ids),
                    month_end.isoformat(),
                )
                # Also save ID database
                id_db_file = metadata_dir / "collected_ids.json"
                save_collected_ids(id_db_file, collected_ids)
                
                # Incrementally save metadata
                if new_records:
                    write_output(
                        session=session,
                        records=new_records,
                        output_root=output_root,
                        query="all_papers",
                        size=size,
                        order=order,
                        searchtype=searchtype,
                        abstracts=abstracts,
                        source=source,
                        append=True,
                    )
            
            print(f"\nCollected {len(new_records)} new papers for {month_start.strftime('%Y-%m')} (total: {len(collected_ids)})")
            
        except Exception as exc:
            print(f"\nError collecting papers for {month_start.strftime('%Y-%m')}: {exc}")
            if checkpoint_file and not dry_run:
                save_checkpoint(
                    checkpoint_file,
                    month_start.isoformat(),
                    0,
                    0,
                    len(collected_ids),
                    checkpoint_data.get("last_successful_date") if checkpoint_data else None,
                )
            # Wait before continuing
            time.sleep(10)
            # Move to next month on error
            date_iter = month_end
            progress.update(1)
            continue
        
        # Move to next month
        date_iter = month_end
        progress.update(1)
        # Small delay to be respectful
        time.sleep(2)
    
    progress.close()
    
    if not dry_run:
        print(f"\nTotal papers collected: {len(collected_ids)}")
        print(f"Data saved under {base_dir}")
    else:
        print(f"\nDry run complete. Would collect {len(collected_ids)} unique papers.")


def main() -> None:
    load_dotenv()
    args = parse_args()

    output_root = args.output if args.output else OUTPUT_DIR
    output_root.mkdir(parents=True, exist_ok=True)

    # Handle collect-all mode
    if args.collect_all:
        # Parse dates
        if args.start_date:
            start_date = datetime.fromisoformat(args.start_date).replace(tzinfo=timezone.utc)
        else:
            start_date = ARXIV_START_DATE
        
        if args.end_date:
            end_date = datetime.fromisoformat(args.end_date).replace(tzinfo=timezone.utc)
        else:
            end_date = datetime.now(timezone.utc)
        
        # Setup checkpoint and ID tracking
        base_dir = output_root / "all_papers"
        metadata_dir = base_dir / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        
        checkpoint_file = metadata_dir / "checkpoint.json"
        id_db_file = metadata_dir / "collected_ids.json"
        
        # Load checkpoint and collected IDs
        checkpoint_data = None
        collected_ids = set()
        
        if not args.no_resume:
            checkpoint_data = load_checkpoint(checkpoint_file)
            collected_ids = load_collected_ids(id_db_file)
            if checkpoint_data or collected_ids:
                print(f"Resuming collection. Found {len(collected_ids)} previously collected papers.")
        
        session, initial_referer, initial_sec_fetch_site = build_session()
        
        collect_all_papers_by_date(
            session=session,
            output_root=output_root,
            start_date=start_date,
            end_date=end_date,
            size=args.size,
            order=args.order,
            searchtype=args.searchtype,
            abstracts=args.abstracts,
            source=args.source,
            initial_referer=initial_referer,
            initial_sec_fetch_site=initial_sec_fetch_site,
            collected_ids=collected_ids,
            checkpoint_file=checkpoint_file,
            checkpoint_data=checkpoint_data,
            no_resume=args.no_resume,
            dry_run=args.dry_run,
        )
        return

    # Original query-based mode
    if not args.query:
        print("Please supply --query <your search> or use --collect-all to collect all papers.")
        return

    base_dir = output_root / slugify(args.query)
    metadata_dir = base_dir / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    
    checkpoint_file = metadata_dir / "checkpoint.json"
    id_db_file = metadata_dir / "collected_ids.json"
    
    # Load checkpoint and collected IDs for query mode
    checkpoint_data = None
    collected_ids = set()
    
    if not args.no_resume:
        checkpoint_data = load_checkpoint(checkpoint_file)
        collected_ids = load_collected_ids(id_db_file)

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
        collected_ids=collected_ids,
        checkpoint_file=checkpoint_file if not args.dry_run else None,
        checkpoint_data=checkpoint_data,
    )
    
    # Update collected IDs
    for record in records:
        if record.get("id"):
            collected_ids.add(record.get("id"))

    if args.dry_run:
        print(f"Collected {len(records)} records (dry-run, no files written).")
        for record in records[:5]:
            print(f"{record.get('id')}: {record.get('title')}")
        return

    # Save ID database
    save_collected_ids(id_db_file, collected_ids)

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
        append=bool(collected_ids),
    )
    print(f"Archived {len(records)} records under {base_dir}")


if __name__ == "__main__":
    main()

