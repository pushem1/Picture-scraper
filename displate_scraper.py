#!/usr/bin/env python3
"""
Displate Wallpaper Scraper
==========================
Scrapes high-resolution images (4K+) from wallpaper websites that meet
Displate's minimum upload requirements:
  - Landscape: >= 4060px width x 2900px height
  - Portrait:  >= 2900px width x 4060px height

Supported sources:
  1. Wallhaven  (no API key needed, optional for NSFW)
  2. Pexels     (free API key required)
  3. Unsplash   (free API key required)
  4. Pixabay    (free API key required)
  5. Pinterest  (public board/profile RSS URLs only)

Usage:
  python displate_scraper.py --source wallhaven --query "nature" --pages 5
  python displate_scraper.py --source pexels --query "cyberpunk" --limit 50
  python displate_scraper.py --source all --query "space" --pages 3

Setup:
  pip install requests Pillow rich beautifulsoup4
"""

import os
import sys
import json
import time
import csv
import re
import html
import random
import hashlib
import argparse
import struct
import urllib.parse
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    print("ERROR: 'requests' is required. Install with: pip install requests")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.table import Table
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
    from rich.panel import Panel
    from rich.text import Text
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

try:
    from PIL import Image, ImageFilter, ImageStat
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False


# ── Constants ────────────────────────────────────────────────────────────────

DISPLATE_MIN_LANDSCAPE = (4060, 2900)  # width x height
DISPLATE_MIN_PORTRAIT  = (2900, 4060)  # width x height
RELAXED_MIN_LANDSCAPE = (1500, 1000)  # useful candidate-review threshold
RELAXED_MIN_PORTRAIT  = (1000, 1500)  # useful candidate-review threshold

DOWNLOAD_DIR = "displate_downloads"
LANDSCAPE_DIR = os.path.join(DOWNLOAD_DIR, "landscape")
PORTRAIT_DIR  = os.path.join(DOWNLOAD_DIR, "portrait")
HISTORY_FILE  = os.path.join(DOWNLOAD_DIR, ".download_history.json")
CANDIDATES_JSON = os.path.join(DOWNLOAD_DIR, "candidates_latest.json")
CANDIDATES_CSV = os.path.join(DOWNLOAD_DIR, "candidates_latest.csv")

HEADERS = {
    "User-Agent": "DisplateScraper/1.0 (Educational/Personal Use)"
}

# Rate limiting (seconds between requests)
RATE_LIMITS = {
    "wallhaven": 1.2,
    "pexels": 0.5,
    "unsplash": 1.0,
    "pixabay": 0.5,
    "pinterest": 2.0,
}

console = Console() if RICH_AVAILABLE else None
history_lock = threading.Lock()

COMMERCIAL_RISK_TERMS = {
    "batman", "superman", "spiderman", "spider-man", "marvel", "dc comics",
    "disney", "pixar", "star wars", "star trek", "harry potter", "hogwarts",
    "lord of the rings", "lotr", "game of thrones", "pokemon", "nintendo",
    "mario", "zelda", "naruto", "dragon ball", "one piece", "demon slayer",
    "studio ghibli", "ghibli", "fortnite", "minecraft", "cyberpunk 2077",
    "call of duty", "valorant", "league of legends", "celebrity", "logo",
    "brand logo", "trademark",
}

DEFAULT_HISTORY = {
    "downloaded": [],
    "file_hashes": [],
    "perceptual_hashes": [],
    "skipped": 0,
    "total_downloaded": 0,
    "query_stats": {},
}


# ── Utility Functions ────────────────────────────────────────────────────────

def log(msg, style=""):
    if RICH_AVAILABLE:
        console.print(msg, style=style)
    else:
        print(msg)


def log_error(msg):
    log(f"[!] {msg}", "bold red")


def log_success(msg):
    log(f"[+] {msg}", "bold green")


def log_info(msg):
    log(f"[*] {msg}", "bold cyan")


def log_warn(msg):
    log(f"[~] {msg}", "bold yellow")


def ensure_dirs():
    """Create download directories if they don't exist."""
    os.makedirs(LANDSCAPE_DIR, exist_ok=True)
    os.makedirs(PORTRAIT_DIR, exist_ok=True)


def normalize_history(history):
    """Backfill new history fields for older history files."""
    if not isinstance(history, dict):
        history = {}
    for key, value in DEFAULT_HISTORY.items():
        if key not in history:
            history[key] = value.copy() if isinstance(value, (list, dict)) else value
    return history


def load_history():
    """Load download history to avoid duplicates."""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return normalize_history(json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            log_warn(f"Could not read history file; starting fresh: {e}")
    return normalize_history({})


def save_history(history):
    """Save download history."""
    ensure_dirs()
    tmp_path = f"{HISTORY_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(normalize_history(history), f, indent=2)
    os.replace(tmp_path, HISTORY_FILE)


def is_duplicate(url, history):
    """Check if an image URL was already downloaded."""
    url_hash = hashlib.md5(url.encode()).hexdigest()
    return url_hash in history.get("downloaded", [])


def mark_downloaded(url, history, file_hash=None, perceptual_hash=None):
    """Mark an image URL as downloaded."""
    history = normalize_history(history)
    url_hash = hashlib.md5(url.encode()).hexdigest()
    if url_hash not in history["downloaded"]:
        history["downloaded"].append(url_hash)
    if file_hash and file_hash not in history["file_hashes"]:
        history["file_hashes"].append(file_hash)
    if perceptual_hash and perceptual_hash not in history["perceptual_hashes"]:
        history["perceptual_hashes"].append(perceptual_hash)
    history["total_downloaded"] = history.get("total_downloaded", 0) + 1


def record_query_stat(history, query, found=0, downloaded=0, failed=0, duplicate=0, quality_score=None):
    """Track which queries actually produce useful downloads."""
    if not query:
        return
    history = normalize_history(history)
    stats = history["query_stats"].setdefault(query, {
        "found": 0,
        "downloaded": 0,
        "failed": 0,
        "duplicate": 0,
        "quality_total": 0.0,
        "quality_count": 0,
    })
    stats["found"] += found
    stats["downloaded"] += downloaded
    stats["failed"] += failed
    stats["duplicate"] += duplicate
    if quality_score is not None:
        stats["quality_total"] += float(quality_score)
        stats["quality_count"] += 1


def hash_file(filepath, chunk_size=1024 * 1024):
    """Return a SHA256 hash for exact duplicate detection."""
    digest = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def average_hash(filepath):
    """Return a simple 64-bit perceptual hash, or None without Pillow."""
    if not PIL_AVAILABLE:
        return None
    try:
        with Image.open(filepath) as img:
            img = img.convert("L").resize((8, 8))
            pixels = list(img.getdata())
            avg = sum(pixels) / len(pixels)
            bits = "".join("1" if px >= avg else "0" for px in pixels)
            return f"{int(bits, 2):016x}"
    except Exception:
        return None


def hamming_distance(hex_a, hex_b):
    try:
        return (int(hex_a, 16) ^ int(hex_b, 16)).bit_count()
    except Exception:
        return 64


def has_similar_image(perceptual_hash, history, max_distance=6):
    """Detect near-duplicate images from the stored perceptual hashes."""
    if not perceptual_hash or max_distance < 0:
        return False
    for existing_hash in history.get("perceptual_hashes", []):
        if hamming_distance(perceptual_hash, existing_hash) <= max_distance:
            return True
    return False


def is_commercially_risky_text(text):
    normalized = f" {str(text).lower().replace('-', ' ')} "
    return sorted(
        term for term in COMMERCIAL_RISK_TERMS
        if f" {term.replace('-', ' ')} " in normalized
    )


def get_commercial_risks(result):
    fields = [
        result.get("query", ""),
        result.get("filename", ""),
        result.get("id", ""),
        result.get("photographer", ""),
        result.get("source_page", ""),
    ]
    return is_commercially_risky_text(" ".join(str(field) for field in fields))


def request_with_retries(session, method, url, retries=3, backoff=1.5, retry_statuses=None, **kwargs):
    """Run an HTTP request with basic retry and 429 handling."""
    retry_statuses = retry_statuses or {429, 500, 502, 503, 504}
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.request(method, url, **kwargs)
            if resp.status_code in retry_statuses and attempt < retries:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else backoff ** attempt
                resp.close()
                time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt >= retries:
                break
            time.sleep(backoff ** attempt)
    raise last_error


def unique_filepath(target_dir, filename):
    filepath = os.path.join(target_dir, filename)
    if not os.path.exists(filepath):
        return filepath
    base, ext = os.path.splitext(filename)
    counter = 1
    while os.path.exists(filepath):
        filepath = os.path.join(target_dir, f"{base}_{counter}{ext}")
        counter += 1
    return filepath


def quality_score_image(filepath, expected_orientation="both", relaxed_dimensions=False):
    """Return (score, issues, dimensions) using lightweight Pillow heuristics."""
    if not PIL_AVAILABLE:
        return None, ["Pillow unavailable; quality not scored"], (None, None)

    score = 100
    issues = []
    try:
        with Image.open(filepath) as img:
            width, height = img.size
            meets, _ = meets_displate_requirements(width, height, expected_orientation, relaxed=relaxed_dimensions)
            if not meets:
                mode = "relaxed" if relaxed_dimensions else "Displate"
                return 0, [f"Actual size {width}x{height} does not meet {mode} requirements"], (width, height)

            ratio = width / height if height else 0
            if expected_orientation == "landscape" and not 1.2 <= ratio <= 1.9:
                score -= 10
                issues.append("awkward landscape aspect ratio")
            elif expected_orientation == "portrait" and not 0.52 <= ratio <= 0.9:
                score -= 10
                issues.append("awkward portrait aspect ratio")

            file_mb = os.path.getsize(filepath) / (1024 * 1024)
            megapixels = max((width * height) / 1_000_000, 1)
            kb_per_mp = (file_mb * 1024) / megapixels
            if file_mb < 1.0 or kb_per_mp < 120:
                score -= 18
                issues.append("very small file for its resolution")

            sample = img.convert("L").resize((512, 512))
            stat = ImageStat.Stat(sample)
            contrast = stat.stddev[0]
            if contrast < 25:
                score -= 22
                issues.append("low contrast")
            elif contrast < 40:
                score -= 10
                issues.append("moderate contrast")

            edges = sample.filter(ImageFilter.FIND_EDGES)
            edge_mean = ImageStat.Stat(edges).mean[0]
            if edge_mean < 4:
                score -= 20
                issues.append("likely soft or blurry")
            elif edge_mean < 7:
                score -= 10
                issues.append("possibly soft")

            return max(0, min(100, int(score))), issues, (width, height)
    except Exception as e:
        return 0, [f"could not inspect image: {e}"], (None, None)


def export_candidates(results):
    """Write the latest candidate list for dry-run and metadata-only workflows."""
    ensure_dirs()
    export_rows = []
    for result in results:
        row = {
            "query": result.get("query", ""),
            "source": result.get("source", ""),
            "id": result.get("id", ""),
            "filename": result.get("filename", ""),
            "width": result.get("width", ""),
            "height": result.get("height", ""),
            "orientation": result.get("orientation", ""),
            "url": result.get("url", ""),
            "source_page": result.get("source_page", ""),
            "photographer": result.get("photographer", ""),
            "license": result.get("license", ""),
            "commercial_risks": ", ".join(result.get("commercial_risks", [])),
        }
        export_rows.append(row)

    with open(CANDIDATES_JSON, "w", encoding="utf-8") as f:
        json.dump(export_rows, f, indent=2)

    fieldnames = list(export_rows[0].keys()) if export_rows else [
        "query", "source", "id", "filename", "width", "height", "orientation",
        "url", "source_page", "photographer", "license", "commercial_risks",
    ]
    with open(CANDIDATES_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(export_rows)
    return CANDIDATES_JSON, CANDIDATES_CSV


def meets_displate_requirements(width, height, orientation_filter="both", relaxed=False):
    """
    Check if image dimensions meet Displate requirements.
    Landscape: >= 4060 x 2900
    Portrait:  >= 2900 x 4060
    
    Args:
        width: Image width in pixels
        height: Image height in pixels
        orientation_filter: "landscape", "portrait", or "both"
    
    Returns:
        (meets_requirements: bool, orientation: str or None)
    """
    landscape_min = RELAXED_MIN_LANDSCAPE if relaxed else DISPLATE_MIN_LANDSCAPE
    portrait_min = RELAXED_MIN_PORTRAIT if relaxed else DISPLATE_MIN_PORTRAIT
    is_landscape = width >= landscape_min[0] and height >= landscape_min[1]
    is_portrait = width >= portrait_min[0] and height >= portrait_min[1]
    
    # Apply orientation filter
    if orientation_filter == "landscape":
        return (is_landscape, "landscape") if is_landscape else (False, None)
    elif orientation_filter == "portrait":
        return (is_portrait, "portrait") if is_portrait else (False, None)
    else:  # "both"
        if is_landscape:
            return True, "landscape"
        if is_portrait:
            return True, "portrait"
        return False, None


def get_image_dimensions_from_headers(url, session):
    """
    Try to get image dimensions by downloading only the header bytes.
    Falls back to full download if needed.
    Returns (width, height) or (None, None).
    """
    try:
        resp = session.get(url, headers=HEADERS, stream=True, timeout=15)
        resp.raise_for_status()

        # Read first 32KB to get image header info
        chunk = resp.raw.read(32768)
        resp.close()

        if not chunk:
            return None, None

        # Try to detect PNG
        if chunk[:8] == b'\x89PNG\r\n\x1a\n':
            if len(chunk) >= 24:
                w = struct.unpack('>I', chunk[16:20])[0]
                h = struct.unpack('>I', chunk[20:24])[0]
                return w, h

        # Try to detect JPEG
        if chunk[:2] == b'\xff\xd8':
            return _get_jpeg_dimensions(chunk)

        # Try to detect WebP
        if chunk[:4] == b'RIFF' and chunk[8:12] == b'WEBP':
            if chunk[12:16] == b'VP8 ' and len(chunk) >= 30:
                w = struct.unpack('<H', chunk[26:28])[0] & 0x3FFF
                h = struct.unpack('<H', chunk[28:30])[0] & 0x3FFF
                return w, h
            elif chunk[12:16] == b'VP8L' and len(chunk) >= 25:
                bits = struct.unpack('<I', chunk[21:25])[0]
                w = (bits & 0x3FFF) + 1
                h = ((bits >> 14) & 0x3FFF) + 1
                return w, h

        return None, None

    except Exception:
        return None, None


def _get_jpeg_dimensions(data):
    """Parse JPEG header to extract dimensions."""
    try:
        i = 2
        while i < len(data) - 1:
            if data[i] != 0xFF:
                return None, None
            marker = data[i + 1]
            i += 2

            if marker == 0xD9:  # EOI
                return None, None
            if marker == 0xDA:  # SOS
                return None, None

            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if i + 7 <= len(data):
                    h = struct.unpack('>H', data[i + 3:i + 5])[0]
                    w = struct.unpack('>H', data[i + 5:i + 7])[0]
                    return w, h
                return None, None

            if i + 2 <= len(data):
                length = struct.unpack('>H', data[i:i + 2])[0]
                i += length
            else:
                return None, None
    except Exception:
        return None, None
    return None, None


def download_image(result, history, min_quality=0, similar_threshold=6, retries=3, relaxed_dimensions=False):
    """Download, verify, score, and dedupe an image."""
    url = result["url"]
    filename = result["filename"]
    orientation = result["orientation"]
    session = requests.Session()
    try:
        resp = request_with_retries(
            session,
            "GET",
            url,
            retries=retries,
            headers=HEADERS,
            stream=True,
            timeout=60,
        )

        target_dir = LANDSCAPE_DIR if orientation == "landscape" else PORTRAIT_DIR
        filepath = unique_filepath(target_dir, filename)
        temp_filepath = f"{filepath}.part"

        with open(temp_filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        resp.close()
        os.replace(temp_filepath, filepath)

        quality_score, quality_issues, dimensions = quality_score_image(
            filepath,
            orientation,
            relaxed_dimensions=relaxed_dimensions,
        )
        if quality_score == 0:
            os.remove(filepath)
            with history_lock:
                record_query_stat(history, result.get("query"), failed=1)
            return None, "; ".join(quality_issues), quality_score

        if quality_score is not None and quality_score < min_quality:
            os.remove(filepath)
            with history_lock:
                record_query_stat(history, result.get("query"), failed=1, quality_score=quality_score)
            return None, f"Quality score {quality_score} below minimum {min_quality}: {', '.join(quality_issues)}", quality_score

        file_hash = hash_file(filepath)
        perceptual_hash = average_hash(filepath)

        with history_lock:
            if file_hash in history.get("file_hashes", []):
                os.remove(filepath)
                record_query_stat(history, result.get("query"), duplicate=1)
                return None, "Exact duplicate content", quality_score

            if has_similar_image(perceptual_hash, history, similar_threshold):
                os.remove(filepath)
                record_query_stat(history, result.get("query"), duplicate=1, quality_score=quality_score)
                return None, f"Near-duplicate image hash (threshold {similar_threshold})", quality_score

            mark_downloaded(url, history, file_hash=file_hash, perceptual_hash=perceptual_hash)
            record_query_stat(history, result.get("query"), downloaded=1, quality_score=quality_score)

        result["quality_score"] = quality_score
        result["quality_issues"] = quality_issues
        result["actual_width"], result["actual_height"] = dimensions
        return filepath, None, quality_score

    except Exception as e:
        temp_filepath = locals().get("temp_filepath")
        if temp_filepath and os.path.exists(temp_filepath):
            try:
                os.remove(temp_filepath)
            except OSError:
                pass
        with history_lock:
            record_query_stat(history, result.get("query"), failed=1)
        return None, str(e), None


def sanitize_filename(name):
    """Remove unsafe characters from filenames."""
    keepchars = (' ', '.', '_', '-')
    return "".join(c for c in name if c.isalnum() or c in keepchars).rstrip()[:100]


def split_csv_values(value):
    """Split comma-separated CLI values while keeping empty input harmless."""
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def guess_extension_from_url(url, default="jpg"):
    path = urllib.parse.urlparse(url).path
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return ext if ext in ("jpg", "jpeg", "png", "webp") else default


def normalize_pinterest_feed_url(url):
    """Convert a public Pinterest profile/board URL into its RSS URL."""
    if not url:
        return None
    raw = url.strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = f"https://www.pinterest.com/{raw.lstrip('/')}"

    parsed = urllib.parse.urlparse(raw)
    host = parsed.netloc.lower()
    if "pinterest." not in host:
        return None
    if parsed.path.lower().startswith(("/pin/", "/search/")):
        return None

    clean_path = parsed.path.strip("/")
    if not clean_path:
        return None
    clean_path = clean_path[:-4] if clean_path.endswith(".rss") else clean_path
    return urllib.parse.urlunparse(("https", parsed.netloc or "www.pinterest.com", f"/{clean_path}.rss", "", "", ""))


def load_pinterest_urls(args):
    urls = split_csv_values(getattr(args, "pinterest_url", None))
    urls_file = getattr(args, "pinterest_urls_file", None)
    if urls_file:
        try:
            with open(urls_file, "r", encoding="utf-8") as f:
                urls.extend(line.strip() for line in f if line.strip() and not line.lstrip().startswith("#"))
        except OSError as e:
            log_warn(f"Could not read Pinterest URL file '{urls_file}': {e}")
    return urls


def load_pinterest_search_terms(args):
    terms = split_csv_values(getattr(args, "pinterest_search", None))
    terms_file = getattr(args, "pinterest_search_file", None)
    if terms_file:
        try:
            with open(terms_file, "r", encoding="utf-8") as f:
                terms.extend(line.strip() for line in f if line.strip() and not line.lstrip().startswith("#"))
        except OSError as e:
            log_warn(f"Could not read Pinterest search file '{terms_file}': {e}")
    return terms


def pinterest_search_url(term):
    query = urllib.parse.quote_plus(term.strip())
    return f"https://www.pinterest.com/search/pins/?q={query}&rs=typed"


def extract_urls_from_html_fragment(fragment):
    if not fragment:
        return []
    decoded = html.unescape(fragment)
    return re.findall(r"""(?:src|href)=["']([^"']+)["']""", decoded, flags=re.IGNORECASE)


def extract_pinterest_page_image_urls(page_text):
    """Extract Pinterest CDN image URLs from public search/explore HTML."""
    decoded = html.unescape(page_text or "").replace("\\u002F", "/").replace("\\/", "/")
    patterns = [
        r"https?://i\.pinimg\.com/[^\"'\\<>\s]+?\.(?:jpg|jpeg|png|webp)",
        r"//i\.pinimg\.com/[^\"'\\<>\s]+?\.(?:jpg|jpeg|png|webp)",
    ]
    urls = []
    for pattern in patterns:
        for match in re.findall(pattern, decoded, flags=re.IGNORECASE):
            url = match if match.startswith("http") else f"https:{match}"
            url = url.replace("\\u002F", "/").replace("\\/", "/")
            if url not in urls:
                urls.append(url)
    return urls


def extract_pinterest_pin_links(page_text):
    decoded = html.unescape(page_text or "")
    links = []
    for match in re.findall(r"""(?:https?://www\.pinterest\.[^/"']+)?/pin/(\d+)/?""", decoded):
        link = f"https://www.pinterest.com/pin/{match}/"
        if link not in links:
            links.append(link)
    return links


def pinterest_image_variants(url):
    """Try likely high-resolution variants for Pinterest CDN image URLs."""
    variants = []
    parsed = urllib.parse.urlparse(html.unescape(url))
    if not parsed.scheme:
        return variants

    def add(candidate):
        if candidate not in variants:
            variants.append(candidate)

    if "pinimg.com" not in parsed.netloc.lower():
        add(urllib.parse.urlunparse(parsed))
        return variants

    path = parsed.path
    for size in ("originals", "1200x", "736x", "564x"):
        candidate_path = re.sub(r"/(?:\d+x|originals)/", f"/{size}/", path, count=1)
        add(urllib.parse.urlunparse(parsed._replace(path=candidate_path, query="")))
    add(urllib.parse.urlunparse(parsed._replace(query="")))
    return variants


def choose_pinterest_image_url(urls, session, orientation_filter, relaxed_dimensions=False):
    """Find the first Pinterest image variant that meets Displate dimensions."""
    seen = set()
    for url in urls:
        for candidate in pinterest_image_variants(url):
            if candidate in seen:
                continue
            seen.add(candidate)
            width, height = get_image_dimensions_from_headers(candidate, session)
            if width and height:
                meets, orientation = meets_displate_requirements(
                    width,
                    height,
                    orientation_filter,
                    relaxed=relaxed_dimensions,
                )
                if meets:
                    return candidate, width, height, orientation
    return None, None, None, None


class BaseSourceScraper:
    """Shared source behavior for API-backed image providers."""

    source_id = ""
    display_name = ""
    rate_limit_key = ""

    def __init__(self):
        self.session = requests.Session()
        self.name = self.display_name or self.source_id.title()
        self.retries = 3

    def get_json(self, url, params=None, timeout=20):
        resp = request_with_retries(
            self.session,
            "GET",
            url,
            retries=self.retries,
            params=params,
            headers=HEADERS,
            timeout=timeout,
        )
        try:
            return resp.json()
        finally:
            resp.close()

    def wait_for_rate_limit(self):
        key = self.rate_limit_key or self.source_id
        if key in RATE_LIMITS:
            time.sleep(RATE_LIMITS[key])


# ── Wallhaven Scraper ────────────────────────────────────────────────────────

class WallhavenScraper(BaseSourceScraper):
    """
    Scrapes from Wallhaven.cc API.
    No API key required for SFW content.
    Docs: https://wallhaven.cc/help/api
    """

    BASE_URL = "https://wallhaven.cc/api/v1"
    source_id = "wallhaven"
    display_name = "Wallhaven"

    def __init__(self, api_key=None):
        super().__init__()
        self.api_key = api_key

    def search(self, query, pages=3, categories="111", purity="100", sorting="relevance", orientation_filter="both"):
        """
        Search Wallhaven for images.
        categories: 1=General, 1=Anime, 1=People (e.g. "100" = General only)
        purity: 1=SFW, 0=Sketchy, 0=NSFW (e.g. "100" = SFW only)
        orientation_filter: "landscape", "portrait", or "both"
        """
        results = []

        for page in range(1, pages + 1):
            params = {
                "q": query,
                "categories": categories,
                "purity": purity,
                "atleast": "2900x2900",  # Pre-filter: both dims >= 2900
                "sorting": sorting,
                "order": "desc",
                "page": page,
            }

            if self.api_key:
                params["apikey"] = self.api_key

            try:
                log_info(f"Wallhaven: Fetching page {page}/{pages} for '{query}'...")
                data = self.get_json(f"{self.BASE_URL}/search", params=params)

                wallpapers = data.get("data", [])
                if not wallpapers:
                    log_warn(f"Wallhaven: No more results on page {page}")
                    break

                for wp in wallpapers:
                    width = wp.get("dimension_x", 0)
                    height = wp.get("dimension_y", 0)
                    meets, orientation = meets_displate_requirements(width, height, orientation_filter)
                    
                    if meets:
                        results.append({
                            "url": wp.get("path", ""),
                            "width": width,
                            "height": height,
                            "orientation": orientation,
                            "source": "wallhaven",
                            "id": wp.get("id", ""),
                            "filename": f"wallhaven_{wp.get('id', 'unknown')}.{wp.get('file_type', 'image/jpeg').split('/')[-1]}",
                            "query": query,
                            "source_page": wp.get("url", ""),
                            "license": "Wallhaven source; verify commercial rights before upload",
                        })

                meta = data.get("meta", {})
                current = meta.get("current_page", page)
                last = meta.get("last_page", pages)
                log_info(f"Wallhaven: Found {len([r for r in results if r['source'] == 'wallhaven'])} qualifying images so far (page {current}/{last})")

                if current >= last:
                    break

                self.wait_for_rate_limit()

            except requests.exceptions.RequestException as e:
                log_error(f"Wallhaven API error: {e}")
                break

        return results


# ── Pexels Scraper ───────────────────────────────────────────────────────────

class PexelsScraper(BaseSourceScraper):
    """
    Scrapes from Pexels API.
    Free API key required: https://www.pexels.com/api/
    """

    BASE_URL = "https://api.pexels.com/v1"
    source_id = "pexels"
    display_name = "Pexels"

    def __init__(self, api_key):
        super().__init__()
        self.api_key = api_key
        self.session.headers.update({"Authorization": api_key})

    def search(self, query, pages=3, per_page=40, orientation_filter="both"):
        results = []
        
        for page in range(1, pages + 1):
            params = {
                "query": query,
                "per_page": per_page,
                "page": page,
                "size": "large",  # Filter for large images
            }

            try:
                log_info(f"Pexels: Fetching page {page}/{pages} for '{query}'...")
                data = self.get_json(f"{self.BASE_URL}/search", params=params)

                photos = data.get("photos", [])
                if not photos:
                    log_warn(f"Pexels: No more results on page {page}")
                    break

                for photo in photos:
                    width = photo.get("width", 0)
                    height = photo.get("height", 0)
                    meets, orientation = meets_displate_requirements(width, height, orientation_filter)

                    if meets:
                        # Use the original resolution URL
                        src = photo.get("src", {})
                        original_url = src.get("original", "")

                        if original_url:
                            ext = original_url.split(".")[-1].split("?")[0]
                            if ext not in ("jpg", "jpeg", "png", "webp"):
                                ext = "jpg"

                            results.append({
                                "url": original_url,
                                "width": width,
                                "height": height,
                                "orientation": orientation,
                                "source": "pexels",
                                "id": photo.get("id", ""),
                                "filename": f"pexels_{photo.get('id', 'unknown')}.{ext}",
                                "photographer": photo.get("photographer", "Unknown"),
                                "query": query,
                                "source_page": photo.get("url", ""),
                                "license": "Pexels license; verify current terms before upload",
                            })

                log_info(f"Pexels: Found {len([r for r in results if r['source'] == 'pexels'])} qualifying images so far")

                if not data.get("next_page"):
                    break

                self.wait_for_rate_limit()

            except requests.exceptions.RequestException as e:
                log_error(f"Pexels API error: {e}")
                break

        return results


# ── Unsplash Scraper ─────────────────────────────────────────────────────────

class UnsplashScraper(BaseSourceScraper):
    """
    Scrapes from Unsplash API.
    Free API key required: https://unsplash.com/developers
    """

    BASE_URL = "https://api.unsplash.com"
    source_id = "unsplash"
    display_name = "Unsplash"

    def __init__(self, api_key):
        super().__init__()
        self.api_key = api_key

    def search(self, query, pages=3, per_page=30, orientation_filter="both"):
        results = []
        
        for page in range(1, pages + 1):
            params = {
                "query": query,
                "per_page": per_page,
                "page": page,
                "client_id": self.api_key,
            }

            try:
                log_info(f"Unsplash: Fetching page {page}/{pages} for '{query}'...")
                data = self.get_json(f"{self.BASE_URL}/search/photos", params=params)

                photos = data.get("results", [])
                if not photos:
                    log_warn(f"Unsplash: No more results on page {page}")
                    break

                for photo in photos:
                    width = photo.get("width", 0)
                    height = photo.get("height", 0)
                    meets, orientation = meets_displate_requirements(width, height, orientation_filter)

                    if meets:
                        urls = photo.get("urls", {})
                        raw_url = urls.get("raw", "")

                        if raw_url:
                            results.append({
                                "url": raw_url,
                                "width": width,
                                "height": height,
                                "orientation": orientation,
                                "source": "unsplash",
                                "id": photo.get("id", ""),
                                "filename": f"unsplash_{photo.get('id', 'unknown')}.jpg",
                                "photographer": photo.get("user", {}).get("name", "Unknown"),
                                "query": query,
                                "source_page": photo.get("links", {}).get("html", ""),
                                "license": "Unsplash license; verify current terms before upload",
                            })

                total_pages = data.get("total_pages", pages)
                log_info(f"Unsplash: Found {len([r for r in results if r['source'] == 'unsplash'])} qualifying images so far (page {page}/{total_pages})")

                if page >= total_pages:
                    break

                self.wait_for_rate_limit()

            except requests.exceptions.RequestException as e:
                log_error(f"Unsplash API error: {e}")
                break

        return results


# ── Pixabay Scraper ──────────────────────────────────────────────────────────

class PixabayScraper(BaseSourceScraper):
    """
    Scrapes from Pixabay API.
    Free API key required: https://pixabay.com/api/docs/
    """

    BASE_URL = "https://pixabay.com/api/"
    source_id = "pixabay"
    display_name = "Pixabay"

    def __init__(self, api_key):
        super().__init__()
        self.api_key = api_key

    def search(self, query, pages=3, per_page=50, orientation_filter="both"):
        results = []
        
        for page in range(1, pages + 1):
            params = {
                "key": self.api_key,
                "q": query,
                "per_page": per_page,
                "page": page,
                "min_width": 2900,
                "min_height": 2900,
                "image_type": "photo",
                "safesearch": "true",
            }

            try:
                log_info(f"Pixabay: Fetching page {page}/{pages} for '{query}'...")
                data = self.get_json(self.BASE_URL, params=params)

                photos = data.get("hits", [])
                if not photos:
                    log_warn(f"Pixabay: No more results on page {page}")
                    break

                for photo in photos:
                    width = photo.get("imageWidth", 0)
                    height = photo.get("imageHeight", 0)
                    meets, orientation = meets_displate_requirements(width, height, orientation_filter)

                    if meets:
                        large_url = photo.get("largeImageURL", "")
                        if large_url:
                            ext = large_url.split(".")[-1].split("?")[0]
                            if ext not in ("jpg", "jpeg", "png", "webp"):
                                ext = "jpg"

                            results.append({
                                "url": large_url,
                                "width": width,
                                "height": height,
                                "orientation": orientation,
                                "source": "pixabay",
                                "id": photo.get("id", ""),
                                "filename": f"pixabay_{photo.get('id', 'unknown')}.{ext}",
                                "photographer": photo.get("user", "Unknown"),
                                "query": query,
                                "source_page": photo.get("pageURL", ""),
                                "license": "Pixabay license; verify current terms before upload",
                            })

                total_hits = data.get("totalHits", 0)
                log_info(f"Pixabay: Found {len([r for r in results if r['source'] == 'pixabay'])} qualifying images so far ({total_hits} total matches)")

                if page * per_page >= total_hits:
                    break

                self.wait_for_rate_limit()

            except requests.exceptions.RequestException as e:
                log_error(f"Pixabay API error: {e}")
                break

        return results


# ── Pinterest RSS Scraper ────────────────────────────────────────────────────

class PinterestScraper(BaseSourceScraper):
    """
    Scrapes public Pinterest profile/board RSS feeds and public search pages.
    Search-page scraping is best-effort because Pinterest frequently changes
    logged-out HTML and may return fewer results than the browser UI.
    """

    source_id = "pinterest"
    display_name = "Pinterest"

    def __init__(self, urls=None, search_terms=None, randomize=True, relaxed_dimensions=False):
        super().__init__()
        self.feed_urls = []
        self._searched_feed_urls = set()
        self.search_terms = []
        self._searched_terms = set()
        self.randomize = randomize
        self.relaxed_dimensions = relaxed_dimensions
        for url in urls or []:
            feed_url = normalize_pinterest_feed_url(url)
            if feed_url and feed_url not in self.feed_urls:
                self.feed_urls.append(feed_url)
        for term in search_terms or []:
            term = term.strip()
            if term and term not in self.search_terms:
                self.search_terms.append(term)

    def search(self, query, pages=3, orientation_filter="both"):
        results = []
        query_feeds = []
        query_terms = []

        # Allow direct Pinterest URLs or search terms in -q for one-off use.
        for value in split_csv_values(query):
            feed_url = normalize_pinterest_feed_url(value)
            if feed_url and feed_url not in query_feeds:
                query_feeds.append(feed_url)
            elif value and not urllib.parse.urlparse(value).scheme:
                query_terms.append(value)

        feeds = [feed for feed in (self.feed_urls or query_feeds) if feed not in self._searched_feed_urls]
        terms = [term for term in (self.search_terms or query_terms) if term not in self._searched_terms]
        if not feeds and not terms:
            if not self.feed_urls and not query_feeds and not self.search_terms and not query_terms:
                log_warn("Pinterest requires --pinterest-url/--pinterest-search, their file variants, or a Pinterest URL/search term in -q.")
            return results

        max_items = max(1, pages) * 50
        results.extend(self._search_feeds(feeds, max_items, orientation_filter))
        results.extend(self._search_terms(terms, max_items, orientation_filter))
        return results

    def _search_feeds(self, feeds, max_items, orientation_filter):
        results = []
        for feed_url in feeds:
            try:
                self._searched_feed_urls.add(feed_url)
                log_info(f"Pinterest: Fetching RSS feed {feed_url}...")
                resp = request_with_retries(
                    self.session,
                    "GET",
                    feed_url,
                    retries=self.retries,
                    headers=HEADERS,
                    timeout=25,
                )
                try:
                    root = ET.fromstring(resp.content)
                finally:
                    resp.close()

                items = root.findall(".//item")[:max_items]
                if not items:
                    log_warn(f"Pinterest: No RSS items found in {feed_url}")
                    continue

                for item in items:
                    title = item.findtext("title") or "pinterest_pin"
                    link = item.findtext("link") or feed_url
                    description = item.findtext("description") or ""
                    image_urls = []

                    for child in item.iter():
                        tag = child.tag.lower() if isinstance(child.tag, str) else ""
                        if tag.endswith("content") or tag.endswith("thumbnail") or tag.endswith("enclosure"):
                            candidate = child.attrib.get("url")
                            if candidate:
                                image_urls.append(candidate)

                    image_urls.extend(extract_urls_from_html_fragment(description))
                    image_url, width, height, orientation = choose_pinterest_image_url(
                        image_urls,
                        self.session,
                        orientation_filter,
                        relaxed_dimensions=self.relaxed_dimensions,
                    )
                    if not image_url:
                        continue

                    pin_id = hashlib.md5(link.encode("utf-8")).hexdigest()[:12]
                    ext = guess_extension_from_url(image_url)
                    results.append({
                        "url": image_url,
                        "width": width,
                        "height": height,
                        "orientation": orientation,
                        "source": "pinterest",
                        "id": pin_id,
                        "filename": f"pinterest_{pin_id}.{ext}",
                        "photographer": "Pinterest pin owner / original creator",
                        "query": feed_url,
                        "source_page": link,
                        "license": "Pinterest pin; verify original source and commercial rights before upload",
                        "title": title,
                    })

                log_info(f"Pinterest: Found {len([r for r in results if r['source'] == 'pinterest'])} qualifying images so far")
                self.wait_for_rate_limit()

            except ET.ParseError as e:
                log_error(f"Pinterest RSS parse error for {feed_url}: {e}")
            except requests.exceptions.RequestException as e:
                log_error(f"Pinterest RSS error for {feed_url}: {e}")

        return results

    def _search_terms(self, terms, max_items, orientation_filter):
        results = []
        for term in terms:
            try:
                self._searched_terms.add(term)
                search_url = pinterest_search_url(term)
                log_info(f"Pinterest: Fetching public search results for '{term}'...")
                resp = request_with_retries(
                    self.session,
                    "GET",
                    search_url,
                    retries=self.retries,
                    headers=HEADERS,
                    timeout=25,
                )
                try:
                    page_text = resp.text
                finally:
                    resp.close()

                image_urls = extract_pinterest_page_image_urls(page_text)
                pin_links = extract_pinterest_pin_links(page_text)
                if self.randomize:
                    random.shuffle(image_urls)

                if not image_urls:
                    log_warn(f"Pinterest: No logged-out image candidates found for '{term}'")
                    continue

                accepted = 0
                for image_url in image_urls:
                    if accepted >= max_items:
                        break
                    chosen_url, width, height, orientation = choose_pinterest_image_url(
                        [image_url],
                        self.session,
                        orientation_filter,
                        relaxed_dimensions=self.relaxed_dimensions,
                    )
                    if not chosen_url:
                        continue

                    link = pin_links[accepted % len(pin_links)] if pin_links else search_url
                    pin_id = hashlib.md5(f"{term}:{chosen_url}".encode("utf-8")).hexdigest()[:12]
                    ext = guess_extension_from_url(chosen_url)
                    results.append({
                        "url": chosen_url,
                        "width": width,
                        "height": height,
                        "orientation": orientation,
                        "source": "pinterest",
                        "id": pin_id,
                        "filename": f"pinterest_{pin_id}.{ext}",
                        "photographer": "Pinterest pin owner / original creator",
                        "query": term,
                        "source_page": link,
                        "license": "Pinterest search result; verify original source and commercial rights before upload",
                        "title": term,
                    })
                    accepted += 1

                log_info(f"Pinterest: Found {accepted} qualifying random/search images for '{term}'")
                self.wait_for_rate_limit()

            except requests.exceptions.RequestException as e:
                log_error(f"Pinterest search error for '{term}': {e}")

        return results


# ── Displate Trend Analyzer ──────────────────────────────────────────────────

class DisplateTrendAnalyzer:
    """
    Scrapes Displate.com to analyze trending and bestselling displates.
    Extracts tags, themes, and categories to generate optimal search queries.
    """

    BASE_URL = "https://displate.com"
    TRENDS_CACHE_FILE = os.path.join(DOWNLOAD_DIR, ".displate_trends.json")
    CACHE_DURATION = 3600 * 24  # 24 hours

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def _load_cache(self):
        """Load cached trends if available and not expired."""
        if os.path.exists(self.TRENDS_CACHE_FILE):
            with open(self.TRENDS_CACHE_FILE, "r") as f:
                data = json.load(f)
                timestamp = data.get("timestamp", 0)
                if time.time() - timestamp < self.CACHE_DURATION:
                    return data
        return None

    def _save_cache(self, data):
        """Save trends to cache file."""
        data["timestamp"] = time.time()
        ensure_dirs()
        with open(self.TRENDS_CACHE_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def analyze_trends(self, use_cache=True):
        """
        Analyze Displate marketplace for trending themes and tags.
        Returns a dictionary with recommended search queries.
        """
        if not BS4_AVAILABLE:
            log_error("BeautifulSoup4 is required for trend analysis. Install with: pip install beautifulsoup4")
            return None

        # Try cache first
        if use_cache:
            cached = self._load_cache()
            if cached:
                log_info("Using cached trend data (refresh with --no-cache)")
                return cached

        log_info("Analyzing Displate marketplace for trends...")

        trends_data = {
            "categories": [],
            "trending_tags": [],
            "bestseller_tags": [],
            "color_themes": [],
            "recommended_queries": [],
            "timestamp": time.time()
        }

        # Scrape main categories
        try:
            log_info("Fetching Displate categories...")
            resp = self.session.get(f"{self.BASE_URL}/+shop", timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, 'html.parser')

            # Extract category links
            category_links = soup.find_all('a', href=True)
            categories = set()
            for link in category_links:
                href = link.get('href', '')
                if '/+tag/' in href or '/+theme/' in href or '/category/' in href:
                    category_name = href.split('/')[-1].replace('+', '').replace('-', ' ').strip()
                    if category_name and len(category_name) < 30:
                        categories.add(category_name)

            trends_data["categories"] = sorted(list(categories))[:50]
            log_success(f"Found {len(trends_data['categories'])} categories")

        except Exception as e:
            log_warn(f"Could not fetch categories: {e}")

        # Scrape trending section
        try:
            log_info("Fetching trending displates...")
            resp = self.session.get(f"{self.BASE_URL}/trending", timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, 'html.parser')

            # Extract tags from product titles and descriptions
            product_titles = soup.find_all(['h1', 'h2', 'h3', 'h4'], class_=lambda x: x and 'title' in x.lower() if x else False)
            trending_keywords = set()

            for title in product_titles[:50]:
                text = title.get_text().strip().lower()
                # Split into words and filter
                words = [w.strip() for w in text.split() if len(w.strip()) > 3]
                trending_keywords.update(words)

            # Also check meta tags
            meta_keywords = soup.find_all('meta', attrs={'name': 'keywords'})
            for meta in meta_keywords:
                content = meta.get('content', '')
                keywords = [k.strip() for k in content.split(',') if k.strip()]
                trending_keywords.update(keywords)

            trends_data["trending_tags"] = sorted(list(trending_keywords))[:100]
            log_success(f"Extracted {len(trends_data['trending_tags'])} trending tags")

        except Exception as e:
            log_warn(f"Could not fetch trending section: {e}")

        # Scrape bestsellers
        try:
            log_info("Fetching bestselling displates...")
            resp = self.session.get(f"{self.BASE_URL}/bestsellers", timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, 'html.parser')

            bestseller_keywords = set()
            product_elements = soup.find_all(['div', 'article', 'section'], class_=lambda x: x and 'product' in x.lower() if x else False)

            for element in product_elements[:50]:
                text = element.get_text().strip().lower()
                words = [w.strip() for w in text.split() if 4 < len(w.strip()) < 20]
                bestseller_keywords.update(words[:5])

            trends_data["bestseller_tags"] = sorted(list(bestseller_keywords))[:100]
            log_success(f"Extracted {len(trends_data['bestseller_tags'])} bestseller tags")

        except Exception as e:
            log_warn(f"Could not fetch bestsellers: {e}")

        # Generate recommended search queries
        all_tags = (
            trends_data["categories"] +
            trends_data["trending_tags"][:30] +
            trends_data["bestseller_tags"][:30]
        )

        # Filter and rank by common themes
        common_themes = [
            "space", "nebula", "galaxy", "astronaut", "cyberpunk", "neon", "city",
            "anime", "manga", "samurai", "japanese", "nature", "mountain", "forest",
            "ocean", "wave", "sunset", "dark", "gothic", "skull", "abstract",
            "geometric", "minimal", "colorful", "fantasy", "dragon", "wolf",
            "tiger", "lion", "car", "jdm", "supercar", "gaming", "retro",
            "vintage", "music", "band", "movie", "poster", "art", "painting",
            "landscape", "urban", "architecture", "travel", "culture"
        ]

        matched_queries = []
        for tag in all_tags:
            tag_lower = str(tag).lower()
            for theme in common_themes:
                if theme in tag_lower or tag_lower in theme:
                    matched_queries.append(tag)
                    break

        # Deduplicate and sort by relevance
        seen = set()
        unique_queries = []
        for q in matched_queries:
            q_norm = str(q).lower().strip()
            if q_norm not in seen and len(q_norm) > 3:
                seen.add(q_norm)
                unique_queries.append(q)

        trends_data["recommended_queries"] = unique_queries[:100]

        # Add manual high-performing categories if scraping failed
        if not trends_data["recommended_queries"]:
            log_warn("No tags extracted from scraping. Using fallback popular themes.")
            trends_data["recommended_queries"] = [
                "cyberpunk city neon", "space nebula galaxy", "anime landscape sunset",
                "dark forest moon", "japanese art samurai", "ocean waves sunset",
                "mountain lake reflection", "abstract dark geometric", "skull gothic art",
                "jdm car night", "fantasy dragon fire", "retro vaporwave aesthetic",
                "minimal geometric pattern", "urban architecture night", "wolf forest moon",
                "vintage poster art", "gaming retro pixel", "nature waterfall forest",
                "sci-fi spaceship planet", "aurora borealis night sky"
            ]

        # Save to cache
        self._save_cache(trends_data)

        log_success(f"Analysis complete! Generated {len(trends_data['recommended_queries'])} recommended queries")

        return trends_data

    def display_trends(self, trends_data):
        """Display trends analysis in a formatted table."""
        if not trends_data:
            return

        if RICH_AVAILABLE:
            console.print(Panel(
                "[bold]Displate Marketplace Analysis[/bold]\n\n"
                f"Categories found:    {len(trends_data.get('categories', []))}\n"
                f"Trending tags:       {len(trends_data.get('trending_tags', []))}\n"
                f"Bestseller tags:     {len(trends_data.get('bestseller_tags', []))}\n"
                f"Recommended queries: {len(trends_data.get('recommended_queries', []))}",
                title="Trend Analysis",
                border_style="magenta"
            ))

            # Show top recommended queries
            queries = trends_data.get('recommended_queries', [])[:30]
            if queries:
                table = Table(title="Top 30 Recommended Search Queries", show_lines=True)
                table.add_column("Query", style="cyan")

                # Split into 3 columns for compact display
                chunk_size = 10
                chunks = [queries[i:i+chunk_size] for i in range(0, len(queries), chunk_size)]
                for i in range(max(len(c) for c in chunks)):
                    row = []
                    for chunk in chunks:
                        if i < len(chunk):
                            row.append(chunk[i])
                        else:
                            row.append("")
                    table.add_row(*row)

                console.print(table)
        else:
            print("\n=== Displate Marketplace Analysis ===")
            print(f"Categories: {len(trends_data.get('categories', []))}")
            print(f"Trending tags: {len(trends_data.get('trending_tags', []))}")
            print(f"Recommended queries: {len(trends_data.get('recommended_queries', []))}")
            print("\nTop 30 Recommended Queries:")
            for i, q in enumerate(trends_data.get('recommended_queries', [])[:30], 1):
                print(f"  {i}. {q}")

    def get_auto_queries(self, limit=10):
        """Get auto-generated queries, boosted by previous scraper yield."""
        trends = self.analyze_trends()
        if trends and trends.get("recommended_queries"):
            return rank_queries_by_history(trends["recommended_queries"], load_history())[:limit]
        return []


def rank_queries_by_history(queries, history):
    """Prefer queries that have historically produced downloads and quality."""
    stats = normalize_history(history).get("query_stats", {})

    def score(query):
        query_stats = stats.get(query, {})
        found = max(query_stats.get("found", 0), 1)
        downloaded = query_stats.get("downloaded", 0)
        duplicate = query_stats.get("duplicate", 0)
        failed = query_stats.get("failed", 0)
        quality_count = query_stats.get("quality_count", 0)
        quality_avg = (
            query_stats.get("quality_total", 0.0) / quality_count
            if quality_count else 70
        )
        return (downloaded * 4) + (quality_avg / 20) - (duplicate * 1.5) - failed + (found / 20)

    return sorted(queries, key=score, reverse=True)


# ── Config Management ────────────────────────────────────────────────────────

CONFIG_FILE = os.path.join(DOWNLOAD_DIR, ".scraper_config.json")

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {}

def save_config(config):
    ensure_dirs()
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

def get_api_key(source, args_key=None):
    """Get API key from args, config, or environment variables."""
    if args_key:
        return args_key

    env_map = {
        "pexels": "PEXELS_API_KEY",
        "unsplash": "UNSPLASH_API_KEY",
        "pixabay": "PIXABAY_API_KEY",
        "wallhaven": "WALLHAVEN_API_KEY",
    }

    env_key = os.environ.get(env_map.get(source, ""))
    if env_key:
        return env_key

    config = load_config()
    return config.get(f"{source}_api_key")


def setup_api_keys():
    """Interactive setup for API keys."""
    config = load_config()

    log("\n")
    if RICH_AVAILABLE:
        console.print(Panel(
            "[bold]API Key Setup[/bold]\n\n"
            "Enter your API keys below. Press Enter to skip a source.\n"
            "Keys are saved locally and never shared.\n\n"
            "[dim]Get free keys at:[/dim]\n"
            "  Wallhaven:  https://wallhaven.cc/settings/account  (optional)\n"
            "  Pexels:     https://www.pexels.com/api/\n"
            "  Unsplash:   https://unsplash.com/developers\n"
            "  Pixabay:    https://pixabay.com/api/docs/",
            title="Setup",
            border_style="cyan"
        ))
    else:
        print("=== API Key Setup ===")
        print("Enter your API keys below. Press Enter to skip a source.")
        print("Get free keys at:")
        print("  Wallhaven:  https://wallhaven.cc/settings/account  (optional)")
        print("  Pexels:     https://www.pexels.com/api/")
        print("  Unsplash:   https://unsplash.com/developers")
        print("  Pixabay:    https://pixabay.com/api/docs/")

    sources = [
        ("wallhaven", "Wallhaven (optional, for NSFW access)"),
        ("pexels", "Pexels"),
        ("unsplash", "Unsplash"),
        ("pixabay", "Pixabay"),
    ]

    for source, label in sources:
        current = config.get(f"{source}_api_key", "")
        masked = f"{'*' * (len(current) - 4)}{current[-4:]}" if len(current) > 4 else "(not set)"
        key = input(f"\n  {label} API Key [{masked}]: ").strip()
        if key:
            config[f"{source}_api_key"] = key

    save_config(config)
    log_success("Configuration saved!")


# ── Main Orchestrator ────────────────────────────────────────────────────────

def run_scraper(args):
    """Main scraper orchestration."""
    ensure_dirs()
    history = load_history()
    all_results = []
    sources_to_use = []

    if not PIL_AVAILABLE and (args.min_quality > 0 or args.similar_threshold >= 0):
        log_warn("Pillow is not installed, so quality scoring and perceptual dedupe are limited. Install Pillow for full filtering.")

    # Determine which sources to use
    source = args.source.lower()

    if source in ("wallhaven", "all"):
        api_key = get_api_key("wallhaven", getattr(args, "wallhaven_key", None))
        sources_to_use.append(WallhavenScraper(api_key))

    if source in ("pexels", "all"):
        api_key = get_api_key("pexels", getattr(args, "pexels_key", None))
        if api_key:
            sources_to_use.append(PexelsScraper(api_key))
        elif source == "pexels":
            log_error("Pexels API key required. Run with --setup or set PEXELS_API_KEY env var.")
            return

    if source in ("unsplash", "all"):
        api_key = get_api_key("unsplash", getattr(args, "unsplash_key", None))
        if api_key:
            sources_to_use.append(UnsplashScraper(api_key))
        elif source == "unsplash":
            log_error("Unsplash API key required. Run with --setup or set UNSPLASH_API_KEY env var.")
            return

    if source in ("pixabay", "all"):
        api_key = get_api_key("pixabay", getattr(args, "pixabay_key", None))
        if api_key:
            sources_to_use.append(PixabayScraper(api_key))
        elif source == "pixabay":
            log_error("Pixabay API key required. Run with --setup or set PIXABAY_API_KEY env var.")
            return

    pinterest_urls = load_pinterest_urls(args)
    pinterest_search_terms = load_pinterest_search_terms(args)
    if source == "pinterest" or (source == "all" and (pinterest_urls or pinterest_search_terms)):
        sources_to_use.append(PinterestScraper(
            pinterest_urls,
            search_terms=pinterest_search_terms,
            randomize=args.pinterest_random,
            relaxed_dimensions=args.relaxed_dimensions,
        ))

    if not sources_to_use:
        log_error("No valid sources configured. Run with --setup to add API keys.")
        return

    for scraper in sources_to_use:
        scraper.retries = args.retries

    # Show search banner
    raw_queries = [q.strip() for q in args.query.split(",") if q.strip()]
    queries = []
    for query in raw_queries:
        risks = is_commercially_risky_text(query)
        if args.safe_commercial and risks:
            log_warn(f"Skipping commercially risky query '{query}' ({', '.join(risks)})")
            continue
        queries.append(query)

    if not queries:
        log_error("No usable queries after filtering.")
        return

    if RICH_AVAILABLE:
        orientation_display = args.orientation.capitalize() if args.orientation != "both" else "Both (Landscape & Portrait)"
        console.print(Panel(
            f"[bold]Queries:[/bold]       {', '.join(queries)}\n"
            f"[bold]Sources:[/bold]       {', '.join(s.name for s in sources_to_use)}\n"
            f"[bold]Pages/source:[/bold]  {args.pages}\n"
            f"[bold]Orientation:[/bold]   {orientation_display}\n"
            f"[bold]Safe commercial:[/bold] {args.safe_commercial}\n"
            f"[bold]Min quality:[/bold]   {args.min_quality}\n"
            f"[bold]Relaxed dims:[/bold]  {args.relaxed_dimensions}\n"
            f"[bold]Min size:[/bold]      {DISPLATE_MIN_LANDSCAPE[0]}x{DISPLATE_MIN_LANDSCAPE[1]} (landscape) / "
            f"{DISPLATE_MIN_PORTRAIT[0]}x{DISPLATE_MIN_PORTRAIT[1]} (portrait)\n"
            f"[bold]Output:[/bold]        {os.path.abspath(DOWNLOAD_DIR)}\n"
            f"[bold]Threads:[/bold]       {args.threads}",
            title="[bold magenta]Displate Wallpaper Scraper[/bold magenta]",
            border_style="magenta",
        ))
    else:
        print(f"\n{'='*60}")
        print(f"  Displate Wallpaper Scraper")
        print(f"{'='*60}")
        print(f"  Queries:      {', '.join(queries)}")
        print(f"  Sources:      {', '.join(s.name for s in sources_to_use)}")
        print(f"  Pages/source: {args.pages}")
        print(f"  Safe mode:    {args.safe_commercial}")
        print(f"  Min quality:  {args.min_quality}")
        print(f"  Relaxed dims: {args.relaxed_dimensions}")
        print(f"  Output:       {os.path.abspath(DOWNLOAD_DIR)}")
        print(f"{'='*60}\n")

    # ── Phase 1: Search ──
    log_info("Phase 1: Searching for high-resolution images...\n")

    for query in queries:
        for scraper in sources_to_use:
            try:
                results = scraper.search(query, pages=args.pages, orientation_filter=args.orientation)
                with history_lock:
                    record_query_stat(history, query, found=len(results))
                all_results.extend(results)
                log_success(f"{scraper.name}: Found {len(results)} images for '{query}' meeting Displate requirements")
            except Exception as e:
                log_error(f"{scraper.name} error for '{query}': {e}")
        print()

    # Filter commercial-risk candidates when requested.
    if args.safe_commercial:
        safe_results = []
        risk_skipped = 0
        for result in all_results:
            risks = get_commercial_risks(result)
            result["commercial_risks"] = risks
            if risks:
                risk_skipped += 1
                continue
            safe_results.append(result)
        all_results = safe_results
        if risk_skipped:
            log_warn(f"Safe-commercial mode skipped {risk_skipped} risky candidates.")
    else:
        for result in all_results:
            result["commercial_risks"] = get_commercial_risks(result)

    # Deduplicate by URL
    seen_urls = set()
    unique_results = []
    for r in all_results:
        if r["url"] not in seen_urls and not is_duplicate(r["url"], history):
            seen_urls.add(r["url"])
            unique_results.append(r)

    skipped_dupes = len(all_results) - len(unique_results)

    if not unique_results:
        log_warn("No new images found matching Displate requirements.")
        log_info(f"Skipped {skipped_dupes} duplicates from previous downloads.")
        return

    # Summary table
    landscape_count = sum(1 for r in unique_results if r["orientation"] == "landscape")
    portrait_count = sum(1 for r in unique_results if r["orientation"] == "portrait")

    if RICH_AVAILABLE:
        table = Table(title="Search Results Summary")
        table.add_column("Metric", style="cyan")
        table.add_column("Count", style="green", justify="right")
        table.add_row("Total found", str(len(all_results)))
        table.add_row("Duplicates skipped", str(skipped_dupes))
        table.add_row("New to download", str(len(unique_results)))
        table.add_row("Landscape", str(landscape_count))
        table.add_row("Portrait", str(portrait_count))
        console.print(table)
        print()

    # Apply limit
    if args.limit and args.limit < len(unique_results):
        unique_results = unique_results[:args.limit]
        log_info(f"Limited to {args.limit} downloads as requested.")

    if args.dry_run or args.metadata_only:
        json_path, csv_path = export_candidates(unique_results)
        if RICH_AVAILABLE:
            preview = Table(title="Candidate Preview")
            preview.add_column("Source", style="cyan")
            preview.add_column("Query")
            preview.add_column("Size", justify="right")
            preview.add_column("Orientation")
            preview.add_column("Filename")
            for result in unique_results[:20]:
                preview.add_row(
                    result.get("source", ""),
                    result.get("query", ""),
                    f"{result.get('width')}x{result.get('height')}",
                    result.get("orientation", ""),
                    result.get("filename", ""),
                )
            console.print(preview)
        else:
            for result in unique_results[:20]:
                print(f"  {result['source']} {result['width']}x{result['height']} {result['filename']}")
        log_success(f"Saved candidate metadata to {os.path.abspath(json_path)} and {os.path.abspath(csv_path)}")
        if args.dry_run:
            log_info("Dry run complete; no images downloaded.")
        else:
            log_info("Metadata-only run complete; no images downloaded.")
        save_history(history)
        return

    # ── Phase 2: Download ──
    log_info(f"Phase 2: Downloading {len(unique_results)} images...\n")

    downloaded = 0
    failed = 0

    if RICH_AVAILABLE:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("{task.completed}/{task.total}"),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Downloading...", total=len(unique_results))

            def download_one(result):
                filepath, error, quality_score = download_image(
                    result,
                    history,
                    min_quality=args.min_quality,
                    similar_threshold=args.similar_threshold,
                    retries=args.retries,
                    relaxed_dimensions=args.relaxed_dimensions,
                )
                return result, filepath, error, quality_score

            with ThreadPoolExecutor(max_workers=args.threads) as executor:
                futures = {executor.submit(download_one, r): r for r in unique_results}

                for future in as_completed(futures):
                    result, filepath, error, quality_score = future.result()
                    progress.advance(task)

                    if filepath:
                        downloaded += 1
                        size_mb = os.path.getsize(filepath) / (1024 * 1024)
                        quality_label = f" q={quality_score}" if quality_score is not None else ""
                        progress.console.print(
                            f"  [green]OK[/green] {result['filename']} "
                            f"({result['width']}x{result['height']}) "
                            f"[dim]{size_mb:.1f}MB - {result['orientation']}{quality_label}[/dim]"
                        )
                    else:
                        failed += 1
                        if error:
                            progress.console.print(
                                f"  [red]FAIL[/red] {result['filename']}: {error}"
                            )
    else:
        for i, result in enumerate(unique_results, 1):
            print(f"  [{i}/{len(unique_results)}] Downloading {result['filename']}...", end=" ")
            filepath, error, quality_score = download_image(
                result,
                history,
                min_quality=args.min_quality,
                similar_threshold=args.similar_threshold,
                retries=args.retries,
                relaxed_dimensions=args.relaxed_dimensions,
            )
            if filepath:
                downloaded += 1
                size_mb = os.path.getsize(filepath) / (1024 * 1024)
                quality_label = f", q={quality_score}" if quality_score is not None else ""
                print(f"OK ({result['width']}x{result['height']}, {size_mb:.1f}MB{quality_label})")
            else:
                failed += 1
                print(f"FAILED: {error}")

    # Save history
    save_history(history)

    # ── Final Summary ──
    print()
    if RICH_AVAILABLE:
        summary = Table(title="Download Complete", show_header=False, border_style="green")
        summary.add_column("Label", style="bold")
        summary.add_column("Value", style="green")
        summary.add_row("Downloaded", str(downloaded))
        summary.add_row("Failed", str(failed))
        summary.add_row("Landscape folder", os.path.abspath(LANDSCAPE_DIR))
        summary.add_row("Portrait folder", os.path.abspath(PORTRAIT_DIR))
        summary.add_row("Total all-time", str(history.get("total_downloaded", 0)))
        console.print(summary)
    else:
        print(f"  Downloaded: {downloaded}")
        print(f"  Failed: {failed}")
        print(f"  Landscape: {os.path.abspath(LANDSCAPE_DIR)}")
        print(f"  Portrait: {os.path.abspath(PORTRAIT_DIR)}")

    log_success("\nDone! Images are sorted into landscape/ and portrait/ folders, ready for Displate upload.")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scrape high-res wallpapers for Displate uploads",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --setup                                        # Configure API keys
  %(prog)s --analyze-trends                               # Analyze Displate marketplace trends
  %(prog)s --auto-query -s all -p 5                       # Use AI-generated trending queries
  %(prog)s --auto-query --safe-commercial --dry-run        # Preview safer candidates only
  %(prog)s -q "nature mountains" -s wallhaven -p 5        # Search Wallhaven
  %(prog)s -q "cyberpunk,neon city" -s all -p 3           # All sources, multi-query
  %(prog)s -q "abstract art" -s pexels --limit 20         # Pexels, max 20 images
  %(prog)s -q "dark forest" -s all --min-quality 75       # Reject lower-quality downloads
  %(prog)s -s pinterest --pinterest-url https://www.pinterest.com/user/board/ --dry-run
  %(prog)s -s pinterest -q "dark fantasy portrait" -o portrait --dry-run
  %(prog)s -q "anime landscape" -s wallhaven -p 10        # Anime wallpapers
  %(prog)s -q "space" -s all -p 5 -o landscape            # Landscape only (4060x2900+)
  %(prog)s -q "portrait art" -s all -p 5 -o portrait      # Portrait only (2900x4060+)

Minimum dimensions for Displate:
  Landscape:  4060 x 2900 px
  Portrait:   2900 x 4060 px

Supported sources:
  wallhaven   - No API key needed (SFW). Key needed for NSFW.
  pexels      - Free API key: https://www.pexels.com/api/
  unsplash    - Free API key: https://unsplash.com/developers
  pixabay     - Free API key: https://pixabay.com/api/docs/
  pinterest   - Public Pinterest profile/board RSS URLs and best-effort public search pages
  all         - Search all configured sources
        """,
    )

    parser.add_argument(
        "--setup", action="store_true",
        help="Interactive API key setup"
    )
    parser.add_argument(
        "--analyze-trends", action="store_true",
        help="Analyze Displate marketplace for trending themes and tags"
    )
    parser.add_argument(
        "--auto-query", action="store_true",
        help="Use auto-generated queries from Displate trend analysis"
    )
    parser.add_argument(
        "--auto-query-limit", type=int, default=10,
        help="Number of auto-generated queries to use. Default: 10"
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Force refresh of trend data (ignore cache)"
    )
    parser.add_argument(
        "-q", "--query", type=str, default=None,
        help="Search query (comma-separated for multiple). Use --auto-query for automatic"
    )
    parser.add_argument(
        "-s", "--source", type=str, default="wallhaven",
        choices=["wallhaven", "pexels", "unsplash", "pixabay", "pinterest", "all"],
        help="Image source to scrape. Default: wallhaven"
    )
    parser.add_argument(
        "--pinterest-url", type=str, default=None,
        help="Comma-separated public Pinterest profile/board URLs. Used with -s pinterest or -s all"
    )
    parser.add_argument(
        "--pinterest-urls-file", type=str, default=None,
        help="Text file containing public Pinterest profile/board URLs, one per line"
    )
    parser.add_argument(
        "--pinterest-search", type=str, default=None,
        help="Comma-separated Pinterest search terms for public logged-out search scraping"
    )
    parser.add_argument(
        "--pinterest-search-file", type=str, default=None,
        help="Text file containing Pinterest search terms, one per line"
    )
    parser.add_argument(
        "--pinterest-random", dest="pinterest_random", action="store_true", default=True,
        help="Shuffle Pinterest search candidates before filtering. Default: true"
    )
    parser.add_argument(
        "--no-pinterest-random", dest="pinterest_random", action="store_false",
        help="Keep Pinterest search candidates in page order"
    )
    parser.add_argument(
        "-p", "--pages", type=int, default=3,
        help="Number of pages to fetch per source. Default: 3"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximum number of images to download total"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Search, dedupe, export candidates, and exit without downloading"
    )
    parser.add_argument(
        "--metadata-only", action="store_true",
        help="Write candidates_latest.json/csv and exit without downloading"
    )
    parser.add_argument(
        "--safe-commercial", action="store_true",
        help="Skip queries and candidates containing common trademark/IP risk terms"
    )
    parser.add_argument(
        "--min-quality", type=int, default=0,
        help="Reject downloads below this 0-100 quality score. Default: 0"
    )
    parser.add_argument(
        "--relaxed-dimensions", action="store_true",
        help="Use lower candidate-review dimensions: portrait 1000x1500, landscape 1500x1000"
    )
    parser.add_argument(
        "--similar-threshold", type=int, default=6,
        help="Perceptual hash distance for near-duplicate rejection; use -1 to disable. Default: 6"
    )
    parser.add_argument(
        "--retries", type=int, default=3,
        help="HTTP retry attempts for API requests and downloads. Default: 3"
    )
    parser.add_argument(
        "-t", "--threads", type=int, default=4,
        help="Number of download threads. Default: 4"
    )
    parser.add_argument(
        "-o", "--orientation", type=str, default="both",
        choices=["landscape", "portrait", "both"],
        help="Filter by orientation: landscape (4060x2900+), portrait (2900x4060+), or both. Default: both"
    )
    parser.add_argument(
        "--pexels-key", type=str, default=None,
        help="Pexels API key (or set PEXELS_API_KEY env var)"
    )
    parser.add_argument(
        "--unsplash-key", type=str, default=None,
        help="Unsplash API key (or set UNSPLASH_API_KEY env var)"
    )
    parser.add_argument(
        "--pixabay-key", type=str, default=None,
        help="Pixabay API key (or set PIXABAY_API_KEY env var)"
    )
    parser.add_argument(
        "--wallhaven-key", type=str, default=None,
        help="Wallhaven API key for NSFW access (or set WALLHAVEN_API_KEY env var)"
    )

    args = parser.parse_args()

    if not 0 <= args.min_quality <= 100:
        log_error("--min-quality must be between 0 and 100.")
        return
    if args.threads < 1:
        log_error("--threads must be at least 1.")
        return
    if args.retries < 1:
        log_error("--retries must be at least 1.")
        return
    if args.auto_query_limit < 1:
        log_error("--auto-query-limit must be at least 1.")
        return

    if RICH_AVAILABLE:
        console.print(Text("""
    ____  _           __      __          _____                                
   / __ \(_)______  / /___ _/ /____     / ___/______________ _____  ___  _____
  / / / / / ___/ _ \/ / __ `/ __/ _ \   \__ \/ ___/ ___/ __ `/ __ \/ _ \/ ___/
 / /_/ / (__  )  __/ / /_/ / /_/  __/  ___/ / /__/ /  / /_/ / /_/ /  __/ /    
/_____/_/____/\___/_/\__,_/\__/\___/  /____/\___/_/   \__,_/ .___/\___/_/     
                                                           /_/                 
        """, style="bold magenta"))

    if args.setup:
        setup_api_keys()
        return

    # Handle trend analysis
    if args.analyze_trends:
        analyzer = DisplateTrendAnalyzer()
        trends = analyzer.analyze_trends(use_cache=not args.no_cache)
        if trends:
            analyzer.display_trends(trends)
            
            # Save to file for easy access
            trends_file = os.path.join(DOWNLOAD_DIR, "recommended_queries.txt")
            ensure_dirs()
            with open(trends_file, "w") as f:
                f.write("# Recommended Search Queries (Based on Displate Trends)\n")
                f.write(f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                for q in trends.get("recommended_queries", []):
                    f.write(f"{q}\n")
            log_success(f"\nSaved queries to: {os.path.abspath(trends_file)}")
            
            print("\nTo use these queries, run:")
            print(f"  python {sys.argv[0]} --auto-query -s all -p 5")
        return

    # Handle auto-query mode
    if args.auto_query:
        analyzer = DisplateTrendAnalyzer()
        auto_queries = analyzer.get_auto_queries(limit=args.auto_query_limit)
        if auto_queries:
            args.query = ",".join(auto_queries[:args.auto_query_limit])
            log_info(f"Using auto-generated queries: {args.query}\n")
        else:
            log_error("Could not generate auto queries. Please use -q to specify manually.")
            return
    elif not args.query:
        if args.source == "pinterest" and (
            args.pinterest_url or args.pinterest_urls_file or
            args.pinterest_search or args.pinterest_search_file
        ):
            args.query = args.pinterest_url or args.pinterest_search or "pinterest"
        else:
            log_error("No query specified. Use -q 'your query' or --auto-query for automatic.")
            return

    run_scraper(args)


if __name__ == "__main__":
    main()
