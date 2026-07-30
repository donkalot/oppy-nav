# oppy-nav

Op shop route planner for Australia. Plan a road trip, see every op shop along the corridor, pick your stops, and hand off to Google Maps for navigation.

**Live app:** https://donkalot.github.io/oppy-nav/

## What it does

- Enter a start and end address, set a corridor width (0.5–10 km)
- App draws the driving route and highlights every op shop inside the corridor plus a radius around each endpoint
- Filter by chain (Vinnies, Salvos, Red Cross, Lifeline, Anglicare, …) and by "open now"
- Auto-pick the N shops closest to your route, or hand-pick from the list
- Plan an optimised trip through your selected stops (TSP order) and open the result in Google Maps
- Installable as a PWA on Android and iOS

## Data

~1,700 shops across Australia, merged from four sources and deduped by proximity + chain:

| Source           | Count | How                                                             |
| ---------------- | ----- | --------------------------------------------------------------- |
| Vinnies          | ~450  | Scraped from `vinnies.org.au` sitemap + embedded Next.js data   |
| OpenStreetMap    | ~750  | Overpass query for `shop=charity` in AU                          |
| Salvos           | ~310  | Salvos Saleor GraphQL API + Nominatim geocoding                 |
| Red Cross        | ~180  | Scraped from `redcross.org.au` retail-stores pages              |

Opening hours available for ~450 shops (from Vinnies).

## Auto-refresh

`.github/workflows/refresh-data.yml` runs every Sunday and:

1. Re-scrapes all four sources
2. Geocodes any new Salvos addresses (cached in `data/salvos_geocoded.json`)
3. Dedupes and rebuilds the `SHOPS` blob inside `index.html`
4. Refuses to publish if any source drops >25% below baseline (`MIN_COUNTS` in `scripts/scrape_all.py`)
5. Commits and pushes; GitHub Pages redeploys automatically

Manual trigger:

```bash
gh workflow run refresh-data.yml
```

## Development

Single HTML file, no build step. Just open `index.html` (or serve locally to test the service worker):

```bash
python -m http.server 8000
```

### Tests

Runs automatically on every push (`.github/workflows/test.yml`). Locally:

```bash
# Unit tests (dedupe, distance, compact records, quality guards)
pip install pytest requests
pytest scripts/test_scrape.py

# Smoke test (serves the app, drives a headless browser)
pip install -r tests/requirements.txt
playwright install chromium
pytest tests/test_smoke.py
```

### Running the scraper manually

```bash
pip install -r scripts/requirements.txt
python scripts/scrape_all.py
```

Salvos geocoding respects Nominatim's 1 req/sec limit — a cold run takes ~10 min for the ~470 addresses. Subsequent runs only geocode new stores (cache is warm).

## Stack

- Leaflet + Turf.js for map + corridor buffering
- OSRM public API for routing and TSP-style trip optimisation
- Nominatim for geocoding
- Vanilla JS, no framework
- PWA via `manifest.json` + `sw.js`
- Data pipeline in Python 3.12, running weekly in GitHub Actions

## Support

If oppy-nav saved you a detour: [buy me a coffee](https://ko-fi.com/donkware) ☕
