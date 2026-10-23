# Displate Scraper

Scrapes high-resolution image candidates from Wallhaven, Pexels, Unsplash, Pixabay, and public Pinterest board/profile RSS feeds, then filters them against Displate-oriented dimensions.

## Setup

```bash
pip install -r requirements.txt
python displate_scraper.py --setup
```

## Common Runs

```bash
python displate_scraper.py -q "nature mountains" -s all -p 5
python displate_scraper.py --auto-query -s all -p 5 --limit 50
python displate_scraper.py --auto-query --safe-commercial --dry-run
python displate_scraper.py -q "dark forest" -s all --min-quality 75
python displate_scraper.py -s pinterest --pinterest-url https://www.pinterest.com/user/board/ --dry-run
python displate_scraper.py -s pinterest -q "dark fantasy portrait" -o portrait --dry-run
python displate_scraper.py -s pinterest --pinterest-search "dark fantasy portrait,botanical poster" -o portrait --limit 40 --min-quality 75 --relaxed-dimensions
```

## New Safety And Quality Features

- URL, SHA256, and perceptual-hash dedupe.
- Retry/backoff with `429` handling and `.part` downloads.
- Optional quality rejection with `--min-quality`.
- Commercial-risk query and candidate filtering with `--safe-commercial`.
- Dry-run and metadata-only modes that export `displate_downloads/candidates_latest.json` and `.csv`.
- Auto-query ranking that learns from historical query yield and quality.
- Pinterest support through public profile/board RSS URLs using `-s pinterest --pinterest-url ...`.
- Pinterest random/search discovery with `-s pinterest -q "topic"` or `--pinterest-search "topic one,topic two"`.
- `--relaxed-dimensions` for candidate discovery when a source rarely exposes full Displate-size originals.

`--safe-commercial` is a heuristic filter, not legal clearance. Verify source licenses and rights before uploading any image commercially.

Pinterest pins commonly point to third-party copyrighted work. Treat Pinterest as a discovery/review source, not a rights-cleared stock source. Search discovery depends on Pinterest's logged-out HTML and can return fewer results than the browser UI.
