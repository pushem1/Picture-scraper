  ### Step 1: Run With Multiple Queries Across All Sources 
 # python displate_scraper.py \
 # -q "nature mountains,cyberpunk city,space nebula,dark fantasy,samurai art,ocean waves,forest fog,neon lights,abstract dark,japanese art" \
 # -s all \
 # -p 10 \
 # -t 6


  ### Step 2: Run Targeted Niche Batches
# Anime / Manga (Wallhaven is best for this)
py displate_scraper.py -q "anime landscape,anime cityscape,studio ghibli,anime sunset,anime night sky" -s wallhaven -p 15

# Dark / Moody (sells great on Displate)
py displate_scraper.py -q "dark aesthetic,gothic architecture,animals,skull art" -s pexels -p 15 --limit 5 -t 4

# Space / Sci-Fi
python displate_scraper.py -q "nebula,galaxy,astronaut,spaceship,alien planet,sci-fi landscape" -s all -p 8

# Nature / Landscape (huge supply)
python displate_scraper.py -q "mountain lake,aurora borealis,volcano,waterfall,desert dunes,canyon" -s all -p 10

# Cars / Urban
python displate_scraper.py -q "jdm car night,supercar rain,tokyo street,neon city rain,urban night" -s all -p 8


### Step 4: Re-run Safely Anytime
# Safe to repeat -- only fetches new images
python displate_scraper.py -q "nature mountains" -s all -p 10




# Quick Reference of All Options
-q, --query          Search terms (comma-separated)
-s, --source         wallhaven | pexels | unsplash | pixabay | all
-p, --pages          Pages per source per query (default: 3)
-t, --threads        Parallel download threads (default: 4, recommended: 4-6)
--limit              Cap total downloads (e.g. --limit 100)
--setup              Interactive API key wizard
--orientation or -o (landscape or portrait)
--wallhaven-key      Pass key inline instead of env var
--pexels-key         Pass key inline instead of env var
--unsplash-key       Pass key inline instead of env var
--pixabay-key        Pass key inline instead of env var




# Analyze what's trending on Displate right now
py displate_scraper.py --analyze-trends

# Use AI-generated trending queries automatically
py displate_scraper.py --auto-query -s all -p 5

# Force refresh trend data (ignores 24hr cache)
py displate_scraper.py --analyze-trends --no-cache




# Preview safer candidates without downloading
py displate_scraper.py --auto-query --safe-commercial --dry-run

# Export candidate metadata only
py displate_scraper.py -q "dark forest,neon city" -s all -p 5 --metadata-only

# Download only images scoring 75+ on the quality heuristic
py displate_scraper.py -q "mountain lake" -s all -p 5 --min-quality 75

# Tune duplicate/retry behavior
py displate_scraper.py -q "space nebula" -s all -p 5 --similar-threshold 4 --retries 5

# Pinterest public board/profile RSS scraping
py displate_scraper.py -s pinterest --pinterest-url https://www.pinterest.com/user/board/ --dry-run
py displate_scraper.py -s pinterest --pinterest-urls-file pinterest_urls.txt --metadata-only

# Pinterest random/search discovery
py displate_scraper.py -s pinterest -q "dark fantasy portrait" -o portrait --dry-run
py displate_scraper.py -s pinterest --pinterest-search "dark fantasy portrait,botanical poster,moody architecture" -o portrait --limit 40 --safe-commercial --min-quality 75 --relaxed-dimensions --similar-threshold 4 --retries 5 -t 2
py displate_scraper.py -s pinterest --pinterest-search-file pinterest_search_terms.txt -o portrait --metadata-only


# New Options
--dry-run            Search, dedupe, export candidates, and exit without downloading
--metadata-only      Save candidates_latest.json/csv and exit without downloading
--safe-commercial    Skip common trademark/IP-risk query terms and candidates
--min-quality        Reject downloads below a 0-100 quality score
--relaxed-dimensions Use lower candidate-review dimensions for sources like Pinterest
--similar-threshold  Near-duplicate perceptual hash distance; -1 disables it
--retries            HTTP retry attempts for API requests and downloads
--auto-query-limit   Number of trend-ranked queries to use
--pinterest-url      Comma-separated public Pinterest profile/board URLs
--pinterest-urls-file Text file of public Pinterest profile/board URLs
--pinterest-search   Comma-separated Pinterest search terms
--pinterest-search-file Text file of Pinterest search terms
--pinterest-random   Shuffle Pinterest search candidates before filtering
--no-pinterest-random Keep Pinterest search candidates in page order
