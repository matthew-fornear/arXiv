<h1 align="center">arXiv Search Collector</h1>

Python utility that replays the provided arXiv search requests, paginates through the result set, stores metadata, and downloads each paper’s PDF into an organised output tree.

## Prerequisites

- Python 3.9+
- Dependencies (install via `pip install -r script/requirements.txt` or individually):
  - `requests`
  - `beautifulsoup4`
  - `python-dotenv`
  - `tqdm`

## Environment

1. Copy `.env.example` to `.env`.
2. Replace the placeholder values with your actual cookies and any header overrides copied from your browser session.
   - `ARXIV_COOKIE` must contain the full cookie header string exactly as captured by your browser or curl.
   - `ARXIV_USER_AGENT` should reflect your browser; leave the provided default if unsure.
   - `ARXIV_REFERER` optionally seeds the first request; subsequent requests update the header automatically. A generic origin such as `https://arxiv.org/` is a safe default.
   - `ARXIV_SEC_FETCH_SITE` defaults to `cross-site` for the first request; the script flips to `same-origin` while paginating.

Keep `.env` out of version control—`.gitignore` already takes care of this.

## Running

### Query-Based Collection

```bash
python3 script/arxivCollector.py --query "blackhole"
```

- `--query` is required for query-based collection; the script exits with a reminder if it is omitted.
- Output root defaults to `/output`.
- Use `--max-pages` to cap pagination, or `--dry-run` to inspect the first page without writing files.
- Adjust parameters such as `--size`, `--order`, `--searchtype`, `--abstracts`, `--source`, and `--output` (base directory) to match other cURL variants.
- arXiv caps pagination at 10,000 results; the CLI automatically stops before exceeding that offset.

### Collecting All Papers

To collect **every paper ever published on arXiv**:

```bash
python3 script/arxivCollector.py --collect-all
```

This will:
- Iterate through all papers from April 1991 (when arXiv started) to today
- Process papers month by month to avoid hitting pagination limits
- Automatically save checkpoints to resume if interrupted
- Prevent duplicates using a persistent ID database
- Download PDFs incrementally

Options for `--collect-all`:
- `--start-date YYYY-MM-DD`: Start from a specific date (default: 1991-04-01)
- `--end-date YYYY-MM-DD`: End at a specific date (default: today)
- `--no-resume`: Start fresh, ignoring any existing checkpoint
- `--dry-run`: Test the collection process without downloading files
- All other parameters (size, order, output, etc.) work as in query mode

**Checkpoint and Resume:**
- The script automatically saves progress in `metadata/checkpoint.json`
- On restart, it automatically resumes from the last successful date
- Collected paper IDs are stored in `metadata/collected_ids.json` to prevent duplicates
- Checkpoints are saved every 100 records and after each month completes
- If the script crashes or is interrupted, simply re-run it to continue

**Example: Collect papers from 2020 onwards:**
```bash
python3 script/arxivCollector.py --collect-all --start-date 2020-01-01
```

To regenerate requirements (optional):

```bash
pip3 install -r script/requirements.txt
```

## Output Format

For a query `blackhole`, results land under `/output/blackhole/`:

- `metadata/results.json`: metadata payload plus one entry per record (ID, title, abstract URL, PDF URL, local file path, and any download error).
- `data/<arxiv-id>.pdf`: cached copy of each paper’s PDF.

Progress bars (via `tqdm`) track both metadata collection and PDF downloads. Delete or relocate previous runs as needed; repeated executions overwrite the metadata file and skip already-downloaded PDFs.

**Note for collect-all mode:** When using `--collect-all`, additional files are created:
- `metadata/collected_ids.json`: database of all collected paper IDs (prevents duplicates across all runs)
- `metadata/checkpoint.json`: checkpoint file tracking progress through date ranges for resuming interrupted collections
- `metadata/results.json`: cumulative metadata file that is appended incrementally (not overwritten)

The script maintains a persistent ID database to avoid downloading the same paper twice, and automatically resumes from checkpoints if interrupted. Simply re-run the same `--collect-all` command to continue from where it left off.

The output root directory (default `/output`) is created automatically, and the entire tree is ignored by git.

### Captcha Note

If arXiv challenges you with a reCAPTCHA, the script will receive the challenge page and thus report zero results. When this happens:

- Solve the captcha in your browser.
- Copy the updated cookie string (including the `captchaAuth=…` token) into `ARXIV_COOKIE` inside `.env`.
- Re-run the script; the next request will succeed once the captcha cookie is present.

