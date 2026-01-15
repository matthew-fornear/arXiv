#!/usr/bin/env python3
"""
Download arXiv search results, collect metadata, and archive full papers using the arXiv API.

This script uses the official arXiv API (http://export.arxiv.org/api/query) which returns
results in Atom XML format. It supports collecting all papers from arXiv with checkpoint/resume
functionality and duplicate prevention.
"""

from __future__ import annotations

import argparse
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlencode, urlparse, quote, quote_plus
from urllib.request import urlopen

from dotenv import load_dotenv
from tqdm import tqdm


# API Configuration
API_BASE_URL = "http://export.arxiv.org/api/query"
OUTPUT_DIR = Path("/home/user/projects/arXiv/output")
# Error logs are stored under the project-level log directory
LOG_DIR = Path(__file__).resolve().parent.parent / "log"
ERROR_LOG_FILE = LOG_DIR / "errors.log"
ARXIV_START_DATE = datetime(1991, 4, 1, tzinfo=timezone.utc)
# Earliest known arXiv submissions were August 1991; avoid querying older months.
ARXIV_EARLIEST_AVAILABLE = datetime(1991, 8, 1, tzinfo=timezone.utc)
CHECKPOINT_SAVE_INTERVAL = 100  # Save checkpoint every N records
API_DELAY = 3  # Seconds to wait between API calls (arXiv recommends 3 seconds)
MAX_RESULTS_PER_CALL = 2000  # API limit per request
MAX_RESULTS_PER_QUERY = 30000  # API limit per query

# XML namespaces
ATOM_NS = "{http://www.w3.org/2005/Atom}"
OPENSEARCH_NS = "{http://a9.com/-/spec/opensearch/1.1/}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect arXiv search results, metadata, and PDFs using the arXiv API."
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
        "--max-results",
        type=int,
        default=MAX_RESULTS_PER_CALL,
        help=f"Maximum results per API call (default: %(default)s, max: {MAX_RESULTS_PER_CALL}).",
    )
    parser.add_argument(
        "--sort-by",
        default="submittedDate",
        choices=("relevance", "lastUpdatedDate", "submittedDate"),
        help="Sort order for results (default: %(default)s).",
    )
    parser.add_argument(
        "--sort-order",
        default="descending",
        choices=("ascending", "descending"),
        help="Sort order direction (default: %(default)s).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Base directory for the output tree (default: /output).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch a sample page and print statistics without writing output.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Don't resume from checkpoint (start fresh).",
    )
    return parser.parse_args()


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
    start: int,
    collected_count: int,
    last_successful_date: Optional[str] = None,
) -> None:
    """Save checkpoint data to resume later."""
    checkpoint_data = {
        "current_date": current_date,
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


def log_error(event: str, context: Optional[Dict] = None) -> None:
    """Append a structured error entry to the log file."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        payload: Dict[str, str] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
        }
        if context:
            payload["context"] = {k: str(v) for k, v in context.items()}
        with ERROR_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
    except Exception as e:
        print(f"Warning: failed to write error log: {e}")


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


def parse_atom_entry(entry: ET.Element) -> Dict[str, str]:
    """Parse an Atom entry element into a record dictionary."""
    record: Dict[str, str] = {}

    # Get arXiv ID from <id> element (format: http://arxiv.org/abs/YYMM.number or old format)
    id_elem = entry.find(f"{ATOM_NS}id")
    if id_elem is not None and id_elem.text:
        abs_url = id_elem.text.strip()
        # Extract ID from URL
        parsed = urlparse(abs_url)
        identifier = parsed.path.rstrip("/").split("/")[-1]
        record["id"] = identifier
        record["abs_url"] = abs_url
        # Construct PDF URL
        if identifier:
            # Handle both old format (hep-ex/0307015) and new format (2301.01234)
            if "/" in identifier:
                record["pdf_url"] = f"https://arxiv.org/pdf/{identifier}.pdf"
            else:
                # New format: YYMM.number
                record["pdf_url"] = f"https://arxiv.org/pdf/{identifier}.pdf"

    # Get title
    title_elem = entry.find(f"{ATOM_NS}title")
    if title_elem is not None and title_elem.text:
        record["title"] = title_elem.text.strip()

    # Get abstract
    summary_elem = entry.find(f"{ATOM_NS}summary")
    if summary_elem is not None and summary_elem.text:
        record["abstract"] = summary_elem.text.strip()

    # Get published date
    published_elem = entry.find(f"{ATOM_NS}published")
    if published_elem is not None and published_elem.text:
        record["published"] = published_elem.text.strip()

    # Get updated date
    updated_elem = entry.find(f"{ATOM_NS}updated")
    if updated_elem is not None and updated_elem.text:
        record["updated"] = updated_elem.text.strip()

    # Get authors
    authors = []
    for author in entry.findall(f"{ATOM_NS}author/{ATOM_NS}name"):
        if author.text:
            authors.append(author.text.strip())
    if authors:
        record["authors"] = ", ".join(authors)

    # Get categories
    categories = []
    for category in entry.findall(f"{ATOM_NS}category"):
        term = category.get("term")
        if term:
            categories.append(term)
    if categories:
        record["categories"] = ", ".join(categories)

    # Get primary category
    primary_cat = entry.find(f"{ARXIV_NS}primary_category")
    if primary_cat is not None:
        term = primary_cat.get("term")
        if term:
            record["primary_category"] = term

    # Get journal reference
    journal_ref = entry.find(f"{ARXIV_NS}journal_ref")
    if journal_ref is not None and journal_ref.text:
        record["journal_ref"] = journal_ref.text.strip()

    # Get DOI
    doi = entry.find(f"{ARXIV_NS}doi")
    if doi is not None and doi.text:
        record["doi"] = doi.text.strip()

    # Get PDF link
    for link in entry.findall(f"{ATOM_NS}link"):
        if link.get("title") == "pdf":
            pdf_url = link.get("href")
            if pdf_url:
                record["pdf_url"] = pdf_url
                break

    return record


def fetch_api_query(params: Dict[str, str]) -> ET.Element:
    """Fetch a query from the arXiv API and return parsed XML."""
    # Build URL with proper encoding
    # The arXiv API examples use literal '+' characters inside date ranges
    # (e.g., submittedDate:[YYYYMMDDHHMM+TO+YYYYMMDDHHMM]). Encoding '+' as
    # %2B causes the API to return HTTP 500, so we explicitly keep '+' unescaped.
    param_parts = []
    for key, value in params.items():
        if key == "search_query":
            # Allow '+' (and other structural characters) to pass through unchanged.
            # Other characters are still percent-encoded.
            encoded_value = quote(value, safe="+:[]()\"")
            param_parts.append(f"{key}={encoded_value}")
        else:
            param_parts.append(f"{key}={quote_plus(str(value))}")
    
    url = "&".join(param_parts)
    full_url = API_BASE_URL + "?" + url
    
    try:
        with urlopen(full_url, timeout=60) as response:
            if response.getcode() != 200:
                raise RuntimeError(f"HTTP {response.getcode()}: {response.msg}")
            xml_data = response.read()
            root = ET.fromstring(xml_data)
            return root
    except Exception as e:
        # Debug: print the URL being requested
        print(f"Debug: Failed URL: {full_url[:400]}...")  # Print first 400 chars
        raise RuntimeError(f"API request failed: {e}")


def collect_records(
    query: str,
    max_results: int = MAX_RESULTS_PER_CALL,
    sort_by: str = "submittedDate",
    sort_order: str = "descending",
    collected_ids: Optional[set[str]] = None,
    checkpoint_file: Optional[Path] = None,
    checkpoint_data: Optional[Dict] = None,
) -> List[Dict[str, str]]:
    """Collect records from arXiv API."""
    collected: List[Dict[str, str]] = []
    # Work on a copy so we don't mutate the caller's collected_ids while iterating.
    seen: set[str] = set(collected_ids) if collected_ids is not None else set()
    start = checkpoint_data.get("start", 0) if checkpoint_data else 0
    records_since_checkpoint = 0

    progress = tqdm(desc="Collecting records", unit="record", total=None)

    while True:
        if start >= MAX_RESULTS_PER_QUERY:
            print(f"\nReached API limit of {MAX_RESULTS_PER_QUERY} results per query.")
            break

        # Build API parameters
        params = {
            "search_query": query,
            "start": str(start),
            "max_results": str(min(max_results, MAX_RESULTS_PER_CALL)),
            "sortBy": sort_by,
            "sortOrder": sort_order,
        }

        try:
            root = fetch_api_query(params)
            
            # Check for errors
            entries = root.findall(f"{ATOM_NS}entry")
            if not entries:
                # Check if it's an error entry
                error_entry = root.find(f"{ATOM_NS}entry")
                if error_entry is not None:
                    error_title = error_entry.find(f"{ATOM_NS}title")
                    error_summary = error_entry.find(f"{ATOM_NS}summary")
                    if error_title is not None and error_title.text == "Error":
                        error_msg = error_summary.text if error_summary is not None else "Unknown error"
                        raise RuntimeError(f"API error: {error_msg}")
                # No entries means no more results
                break

            # Get total results
            total_elem = root.find(f"{OPENSEARCH_NS}totalResults")
            if total_elem is not None and total_elem.text:
                total_results = int(total_elem.text)
                progress.total = min(total_results, MAX_RESULTS_PER_QUERY)
                progress.refresh()

            new_records_in_page = 0
            for entry in entries:
                record = parse_atom_entry(entry)
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
                        None,
                        start + len(entries),
                        len(collected) + len(seen) - (len(collected_ids) if collected_ids else 0),
                    )
                    records_since_checkpoint = 0

            # Check if we got fewer results than requested (last page)
            if len(entries) < max_results:
                break

            # Respect API rate limiting
            time.sleep(API_DELAY)

            start += len(entries)

        except KeyboardInterrupt:
            print("\nInterrupted by user; saving checkpoint before exit...")
            log_error(
                "collect_records_interrupt",
                {
                    "query": query,
                    "start": start,
                    "collected": len(collected),
                },
            )
            if checkpoint_file:
                save_checkpoint(
                    checkpoint_file,
                    None,
                    start,
                    len(collected) + len(seen) - (len(collected_ids) if collected_ids else 0),
                )
            progress.close()
            raise
        except Exception as exc:
            print(f"\nError fetching results at start={start}: {exc}")
            print("Saving checkpoint before retrying...")
            log_error(
                "collect_records_error",
                {
                    "query": query,
                    "start": start,
                    "error": exc,
                },
            )
            if checkpoint_file:
                save_checkpoint(
                    checkpoint_file,
                    None,
                    start,
                    len(collected) + len(seen) - (len(collected_ids) if collected_ids else 0),
                )
            # Wait before retrying
            time.sleep(API_DELAY * 2)
            continue

    progress.close()
    return collected


def download_papers(records: List[Dict[str, str]], data_dir: Path) -> None:
    """Download PDFs for records."""
    data_dir.mkdir(parents=True, exist_ok=True)
    for record in tqdm(records, desc="Downloading PDFs", unit="record"):
        pdf_url = record.get("pdf_url")
        identifier = record.get("id") or ""
        if not pdf_url or not identifier:
            continue

        # Sanitize filename
        filename = identifier.replace("/", "_") + ".pdf"
        destination = data_dir / filename

        if destination.exists():
            record["pdf_local_path"] = str(destination)
            continue

        try:
            with urlopen(pdf_url, timeout=120) as response:
                destination.write_bytes(response.read())
            record["pdf_local_path"] = str(destination)
            time.sleep(0.5)  # Small delay to be respectful
        except Exception as exc:
            record["pdf_download_error"] = str(exc)
            print(f"Error downloading {identifier}: {exc}")
            log_error(
                "download_error",
                {
                    "identifier": identifier,
                    "pdf_url": pdf_url,
                    "error": exc,
                },
            )


def slugify(value: str) -> str:
    """Convert string to filesystem-safe slug."""
    import re
    safe = re.sub(r"\s+", "_", value.strip())
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", safe)
    safe = safe.strip("_.")
    return safe or "search"


def write_output(
    records: List[Dict[str, str]],
    output_root: Path,
    query: str,
    append: bool = False,
) -> Path:
    """Write collected records to output directory."""
    query_slug = slugify(query) if query else "all_papers"
    base_dir = output_root / query_slug
    metadata_dir = base_dir / "metadata"
    data_dir = base_dir / "data"
    metadata_dir.mkdir(parents=True, exist_ok=True)

    download_papers(records, data_dir)

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
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "count": len(records),
        },
        "records": records,
    }
    with metadata_file.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")

    return base_dir


def collect_all_papers_by_date(
    output_root: Path,
    start_date: datetime,
    end_date: datetime,
    max_results: int,
    sort_by: str,
    sort_order: str,
    collected_ids: Optional[set[str]] = None,
    checkpoint_file: Optional[Path] = None,
    checkpoint_data: Optional[Dict] = None,
    no_resume: bool = False,
    dry_run: bool = False,
) -> None:
    """Collect all papers from arXiv by iterating through date ranges."""
    if collected_ids is None:
        collected_ids = set()
    
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

    # Iterate through dates (by month to avoid hitting query limits)
    date_iter = current_date
    progress = tqdm(desc="Collecting all papers", unit="month")

    print(f"Collecting all papers from {start_date.date()} to {end_date.date()}")
    print("Using arXiv API with date range queries...")

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

        # Format dates for API query
        # API format: submittedDate:[YYYYMMDDHHMM+TO+YYYYMMDDHHMM]
        # Use start of month (0000) to end of month (2359) in GMT
        date_from_str = month_start.strftime("%Y%m%d") + "0000"
        date_to_str = (month_end - timedelta(days=1)).strftime("%Y%m%d") + "2359"

        # Build query for date range
        # API format: submittedDate:[YYYYMMDDHHMM+TO+YYYYMMDDHHMM]
        # Note: + signs will be preserved during URL encoding
        query = f"submittedDate:[{date_from_str}+TO+{date_to_str}]"

        # Debug: print first query to see format
        if month_start == current_date:
            print(f"Debug: Query for {month_start.strftime('%Y-%m')}: {query}")
            print(f"Debug: Date range: {date_from_str} to {date_to_str}")

        # Prepare checkpoint data for this date range
        date_checkpoint_data = None
        if checkpoint_file and month_start == current_date:
            date_checkpoint_data = checkpoint_data

        progress.set_description(f"Collecting {month_start.strftime('%Y-%m')}")

        try:
            # Collect records for this date range
            month_records = collect_records(
                query=query,
                max_results=max_results,
                sort_by=sort_by,
                sort_order=sort_order,
                collected_ids=collected_ids,
                checkpoint_file=checkpoint_file,
                checkpoint_data=date_checkpoint_data,
            )

            # Filter out duplicates
            new_records = [r for r in month_records if r.get("id") not in collected_ids]

            # Update collected IDs
            for record in new_records:
                if record.get("id"):
                    collected_ids.add(record.get("id"))

            # Save checkpoint after each month
            if checkpoint_file and not dry_run:
                save_checkpoint(
                    checkpoint_file,
                    month_end.isoformat(),
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
                        records=new_records,
                        output_root=output_root,
                        query="all_papers",
                        append=True,
                    )

            print(f"\nCollected {len(new_records)} new papers for {month_start.strftime('%Y-%m')} (total: {len(collected_ids)})")

        except KeyboardInterrupt:
            print("\nInterrupted by user; saving checkpoint before exit...")
            log_error(
                "collect_month_interrupt",
                {
                    "month": month_start.strftime("%Y-%m"),
                    "collected_total": len(collected_ids),
                },
            )
            if checkpoint_file and not dry_run:
                save_checkpoint(
                    checkpoint_file,
                    month_start.isoformat(),
                    0,
                    len(collected_ids),
                    month_start.isoformat(),
                )
            progress.close()
            raise
        except Exception as exc:
            print(f"\nError collecting papers for {month_start.strftime('%Y-%m')}: {exc}")
            log_error(
                "collect_month_error",
                {
                    "month": month_start.strftime("%Y-%m"),
                    "query": query,
                    "error": exc,
                },
            )
            if checkpoint_file and not dry_run:
                save_checkpoint(
                    checkpoint_file,
                    month_start.isoformat(),
                    0,
                    len(collected_ids),
                    checkpoint_data.get("last_successful_date") if checkpoint_data else None,
                )
            # Wait before continuing
            time.sleep(API_DELAY * 2)
            # Move to next month on error
            date_iter = month_end + timedelta(days=1)
            progress.update(1)
            continue

        # Move to next month
        date_iter = month_end + timedelta(days=1)
        progress.update(1)
        time.sleep(API_DELAY)  # Delay between months

    progress.close()

    if not dry_run:
        total_collected = len(collected_ids) if collected_ids else 0
        print(f"\nTotal papers collected: {total_collected}")
        print(f"Data saved under {base_dir}")
    else:
        total_collected = len(collected_ids) if collected_ids else 0
        print(f"\nDry run complete. Would collect {total_collected} unique papers.")


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

        # Sanity check date bounds
        today = datetime.now(timezone.utc)
        if end_date > today:
            print(f"End date {end_date.date()} is in the future; capping at {today.date()}.")
            end_date = today
        if start_date < ARXIV_EARLIEST_AVAILABLE:
            print(
                f"Start date {start_date.date()} predates earliest arXiv uploads; "
                f"using {ARXIV_EARLIEST_AVAILABLE.date()} instead."
            )
            start_date = ARXIV_EARLIEST_AVAILABLE

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

        collect_all_papers_by_date(
            output_root=output_root,
            start_date=start_date,
            end_date=end_date,
            max_results=args.max_results,
            sort_by=args.sort_by,
            sort_order=args.sort_order,
            collected_ids=collected_ids,
            checkpoint_file=checkpoint_file,
            checkpoint_data=checkpoint_data,
            no_resume=args.no_resume,
            dry_run=args.dry_run,
        )
        return

    # Query-based mode
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

    records = collect_records(
        query=args.query,
        max_results=args.max_results,
        sort_by=args.sort_by,
        sort_order=args.sort_order,
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
        records=records,
        output_root=output_root,
        query=args.query,
        append=bool(collected_ids),
    )
    print(f"Archived {len(records)} records under {base_dir}")


if __name__ == "__main__":
    main()
