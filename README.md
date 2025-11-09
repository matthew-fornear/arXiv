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

```bash
python3 script/arxivCollector.py --query "blackhole"
```

- `--query` is required; the script exits with a reminder if it is omitted.
- Output root defaults to `/output`.
- Use `--max-pages` to cap pagination, or `--dry-run` to inspect the first page without writing files.
- Adjust parameters such as `--size`, `--order`, `--searchtype`, `--abstracts`, `--source`, and `--output` (base directory) to match other cURL variants.
- arXiv caps pagination at 10,000 results; the CLI automatically stops before exceeding that offset.

To regenerate requirements (optional):

```bash
pip3 install -r script/requirements.txt
```

## Output Format

For a query `blackhole`, results land under `/output/blackhole/`:

- `metadata/results.json`: metadata payload plus one entry per record (ID, title, abstract URL, PDF URL, local file path, and any download error).
- `data/<arxiv-id>.pdf`: cached copy of each paper’s PDF.

Progress bars (via `tqdm`) track both metadata collection and PDF downloads. Delete or relocate previous runs as needed; repeated executions overwrite the metadata file and skip already-downloaded PDFs.

The output root directory (default `/output`) is created automatically, and the entire tree is ignored by git.

### Captcha Note

If arXiv challenges you with a reCAPTCHA, the script will receive the challenge page and thus report zero results. When this happens:

- Solve the captcha in your browser.
- Copy the updated cookie string (including the `captchaAuth=…` token) into `ARXIV_COOKIE` inside `.env`.
- Re-run the script; the next request will succeed once the captcha cookie is present.

