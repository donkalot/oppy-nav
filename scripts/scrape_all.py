"""Refresh oppy-nav data: scrape chains + OSM, geocode Salvos, merge, rebuild index.html.

Designed to run in GitHub Actions weekly. Everything is idempotent — only commits when
the resulting HTML actually changes. Salvos geocoding is cached in data/salvos_geocoded.json
to avoid hammering Nominatim on every run.
"""
import json, math, pathlib, re, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
OUT_HTML = ROOT / 'index.html'

UA = 'oppy-nav-refresh (github.com/donkalot/oppy-nav)'
HEADERS = {'User-Agent': UA}

CHAIN_PATTERNS = [
    ('vinn','vinnies'),('vincent','vinnies'),('salv','salvos'),('salvation','salvos'),
    ('red cross','redcross'),('redcross','redcross'),('lifeline','lifeline'),
    ('anglicare','anglicare'),('sacred heart','sacredheart'),('brotherhood','bosl'),
    ('rspca','rspca'),('mission','mission'),('endeavour','endeavour'),('rotary','rotary'),
]


def http_get(url, retries=2, timeout=20):
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
        time.sleep(0.5 * (i + 1))
    return None


# ----- Vinnies -----

def scrape_vinnies():
    print('Vinnies: sitemap...', flush=True)
    txt = http_get('https://www.vinnies.org.au/sitemap.xml', timeout=30)
    urls = sorted(set(re.findall(r'https://[^<]+/shops/[^<]+', txt or '')))
    print(f'  {len(urls)} URLs', flush=True)

    def one(url):
        h = http_get(url)
        if not h: return None
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.DOTALL)
        if not m: return None
        try:
            p = json.loads(m.group(1))['props']['pageProps']['pageData']
        except Exception:
            return None
        coords = ((p.get('location') or {}).get('address') or {}).get('coordinates') or {}
        lat, lng = coords.get('lat'), coords.get('lng')
        if lat is None or lng is None: return None
        hours = {}
        for t in ((p.get('openingHours') or {}).get('openingTimes') or []):
            wd = (t.get('weekday') or '').lower()[:3]
            if t.get('isScheduled') and t.get('open') and t.get('close'):
                hours[wd] = {'o': t['open'], 'c': t['close']}
        return {
            'name': p.get('shopName') or p.get('name') or 'Vinnies',
            'operator': 'Vinnies', 'chain': 'vinnies',
            'lat': round(float(lat), 6), 'lon': round(float(lng), 6),
            'address': p.get('addressLineOne', '') or '',
            'suburb': p.get('addressSuburb', '') or '',
            'state': p.get('addressState', '') or '',
            'postcode': p.get('addressPostcode', '') or '',
            'phone': p.get('phoneNumber', '') or '',
            'hours': hours, 'source': 'vinnies',
        }

    return _parallel(urls, one, 'vinnies')


# ----- Red Cross -----

def scrape_redcross():
    print('Red Cross: sitemap...', flush=True)
    txt = http_get('https://www.redcross.org.au/sitemap.xml', timeout=30)
    urls = sorted(set(re.findall(r'https://[^<]+/retail-stores/[^<]+', txt or '')))
    print(f'  {len(urls)} URLs', flush=True)

    def one(url):
        h = http_get(url)
        if not h: return None
        lat = lon = None
        addr = suburb = state = postcode = phone = ''
        name = 'Red Cross Shop'
        for ld in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', h, re.DOTALL):
            try: obj = json.loads(ld)
            except Exception: continue
            for o in (obj if isinstance(obj, list) else [obj]):
                if not isinstance(o, dict): continue
                t = o.get('@type')
                if isinstance(t, list): t = t[0] if t else ''
                if t not in ('Store','LocalBusiness','Place','ClothingStore','Organization'): continue
                name = o.get('name') or name
                a = o.get('address') or {}
                if isinstance(a, dict):
                    addr = a.get('streetAddress') or addr
                    suburb = a.get('addressLocality') or suburb
                    state = a.get('addressRegion') or state
                    postcode = a.get('postalCode') or postcode
                g = o.get('geo') or {}
                if isinstance(g, dict):
                    try:
                        if g.get('latitude') is not None: lat = float(g['latitude'])
                        if g.get('longitude') is not None: lon = float(g['longitude'])
                    except Exception: pass
                phone = o.get('telephone') or phone
        if lat is None:
            m = re.search(r'"latitude"\s*:\s*"?(-?\d+\.\d+)', h)
            if m: lat = float(m.group(1))
        if lon is None:
            m = re.search(r'"longitude"\s*:\s*"?(1\d+\.\d+)', h)
            if m: lon = float(m.group(1))
        if lat is None or lon is None: return None
        return {
            'name': name.strip(), 'operator': 'Red Cross', 'chain': 'redcross',
            'lat': round(lat, 6), 'lon': round(lon, 6),
            'address': addr, 'suburb': suburb, 'state': state,
            'postcode': str(postcode) if postcode else '', 'phone': phone,
            'hours': {}, 'source': 'redcross',
        }

    return _parallel(urls, one, 'redcross')


def _parallel(urls, fn, label, workers=10):
    out = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, u): u for u in urls}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                r = f.result()
                if r: out.append(r)
            except Exception: pass
            if i % 50 == 0 or i == len(urls):
                print(f'  {label}: {i}/{len(urls)} ({len(out)} valid)', flush=True)
    return out


# ----- Salvos -----

def fetch_salvos_addresses():
    print('Salvos: GraphQL...', flush=True)
    url = 'https://salvos-api-new.annix.com.au/graphql/'
    query = '''
    query($first: Int!, $after: String) {
      warehouses(first: $first, after: $after) {
        totalCount
        pageInfo { hasNextPage endCursor }
        edges { node {
          id name slug
          address { streetAddress1 streetAddress2 city postalCode countryArea phone }
        } }
      }
    }'''
    out = []
    after = None
    while True:
        req = urllib.request.Request(url, data=json.dumps({'query': query, 'variables': {'first': 100, 'after': after}}).encode(), headers={'Content-Type':'application/json','User-Agent':UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        conn = d['data']['warehouses']
        for e in conn['edges']:
            n = e['node']
            a = n['address'] or {}
            out.append({
                'id': n['id'], 'name': n['name'], 'slug': n['slug'],
                'street': (a.get('streetAddress1') or '') + (' ' + a.get('streetAddress2') if a.get('streetAddress2') else ''),
                'city': a.get('city') or '', 'state': a.get('countryArea') or '',
                'postcode': a.get('postalCode') or '', 'phone': a.get('phone') or '',
            })
        if not conn['pageInfo']['hasNextPage']: break
        after = conn['pageInfo']['endCursor']
    print(f'  {len(out)} salvos records', flush=True)
    return out


def geocode_salvos(records, cache_path):
    """Only geocode records whose id isn't already in the cache."""
    cache = {}
    if cache_path.exists():
        for r in json.loads(cache_path.read_text(encoding='utf-8')):
            cache[r['id']] = r
    todo = [s for s in records if s['id'] not in cache or 'lat' not in cache[s['id']]]
    print(f'Salvos geocode: cached={len([r for r in cache.values() if "lat" in r])}, to-geocode={len(todo)}', flush=True)

    def geocode(q):
        u = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode({
            'q': q, 'format': 'json', 'limit': 1, 'countrycodes': 'au'})
        req = urllib.request.Request(u, headers={'User-Agent': UA, 'Accept-Language': 'en-AU'})
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.load(r)
        if not j: return None
        return float(j[0]['lat']), float(j[0]['lon'])

    t0 = time.time()
    for i, s in enumerate(todo, 1):
        parts = [s['street'], s['city'], s['state'], s['postcode']]
        q = ', '.join(p for p in parts if p) + ', Australia'
        coords = None
        for attempt in range(3):
            try:
                coords = geocode(q); break
            except Exception:
                time.sleep(2)
        rec = dict(cache.get(s['id'], s))
        rec.update(s)
        if coords:
            rec['lat'], rec['lon'] = coords
        cache[s['id']] = rec
        if i % 25 == 0 or i == len(todo):
            cache_path.write_text(json.dumps(list(cache.values()), ensure_ascii=False), encoding='utf-8')
            print(f'  {i}/{len(todo)} · {sum(1 for r in cache.values() if "lat" in r)} coords total · {i/(time.time()-t0):.2f}/s', flush=True)
        time.sleep(1.05)

    # Drop cached ids no longer in the current warehouse list
    current_ids = {s['id'] for s in records}
    for stale_id in list(cache.keys()):
        if stale_id not in current_ids:
            del cache[stale_id]
    cache_path.write_text(json.dumps(list(cache.values()), ensure_ascii=False), encoding='utf-8')

    out = []
    for r in cache.values():
        if 'lat' not in r: continue
        out.append({
            'name': r['name'], 'operator': 'Salvos', 'chain': 'salvos',
            'lat': round(float(r['lat']), 6), 'lon': round(float(r['lon']), 6),
            'address': r['street'], 'suburb': r['city'], 'state': r['state'],
            'postcode': str(r['postcode']), 'phone': r['phone'],
            'hours': {}, 'source': 'salvos',
        })
    return out


# ----- OSM -----

def fetch_osm():
    print('OSM: overpass...', flush=True)
    q = '[out:json][timeout:120];area["ISO3166-1"="AU"][admin_level=2]->.au;nwr["shop"="charity"](area.au);out center;'
    endpoints = [
        'https://overpass.kumi.systems/api/interpreter',
        'https://overpass-api.de/api/interpreter',
        'https://overpass.private.coffee/api/interpreter',
    ]
    data = None
    for ep in endpoints:
        try:
            r = requests.post(ep, data={'data': q}, headers=HEADERS, timeout=180)
            if r.status_code == 200:
                data = r.json(); print(f'  fetched from {ep} ({len(data.get("elements", []))} elements)', flush=True); break
            print(f'  {ep} -> {r.status_code}', flush=True)
        except Exception as e:
            print(f'  {ep} err: {e}', flush=True)
    if not data: raise RuntimeError('All Overpass endpoints failed')
    shops = []
    for el in data.get('elements', []):
        if el['type'] == 'node':
            lat, lon = el.get('lat'), el.get('lon')
        else:
            c = el.get('center') or {}
            lat, lon = c.get('lat'), c.get('lon')
        if lat is None or lon is None: continue
        tags = el.get('tags', {}) or {}
        op = tags.get('operator', '') or tags.get('brand', '')
        name = tags.get('name') or op or 'Op shop'
        hay = (name + ' ' + op).lower()
        chain = 'independent'
        for pat, cid in CHAIN_PATTERNS:
            if pat in hay: chain = cid; break
        shops.append({
            'name': name, 'operator': op, 'chain': chain,
            'lat': round(float(lat), 6), 'lon': round(float(lon), 6),
            'address': '', 'suburb': '', 'state': tags.get('addr:state', ''),
            'postcode': tags.get('addr:postcode', ''), 'phone': tags.get('phone', ''),
            'hours': {}, 'source': 'osm',
        })
    print(f'OSM: {len(shops)} shops', flush=True)
    return shops


# ----- Dedupe -----

def dist_km(a, b):
    R = 6371.0
    dlat = math.radians(a['lat']-b['lat']); dlon = math.radians(a['lon']-b['lon'])
    la1 = math.radians(a['lat']); la2 = math.radians(b['lat'])
    h = math.sin(dlat/2)**2 + math.cos(la1)*math.cos(la2)*math.sin(dlon/2)**2
    return 2*R*math.asin(math.sqrt(h))


def dedupe(shops):
    priority = {'vinnies': 0, 'redcross': 1, 'salvos': 2, 'osm': 3}
    shops.sort(key=lambda s: priority.get(s['source'], 9))
    buckets = {}
    kept = []
    for s in shops:
        bk = (round(s['lat']*100), round(s['lon']*100))
        dup = False
        for dy in (-2,-1,0,1,2):
            for dx in (-2,-1,0,1,2):
                for k in buckets.get((bk[0]+dy, bk[1]+dx), []):
                    d = dist_km(k, s)
                    radius = 0.30 if (s['source'] == 'salvos' or k['source'] == 'salvos') else 0.15
                    if d < radius:
                        if s['chain'] == k['chain'] and s['chain'] != 'independent':
                            dup = True; break
                        if s['source'] == 'osm' and k['source'] in ('vinnies','redcross','salvos'):
                            hay = (s['name'] + ' ' + s['operator']).lower()
                            if k['chain'] == 'vinnies' and ('vinn' in hay or 'vincent' in hay): dup = True; break
                            if k['chain'] == 'redcross' and 'red cross' in hay: dup = True; break
                            if k['chain'] == 'salvos' and ('salv' in hay or 'salvation' in hay): dup = True; break
                if dup: break
            if dup: break
        if not dup:
            kept.append(s)
            buckets.setdefault(bk, []).append(s)
    return kept


# ----- Quality guards -----
# Baselines from the first successful run (2026-07-30): vinnies 453, redcross 177,
# salvos 310, osm 757, total kept 1697. Thresholds are ~25% below actuals so upstream
# API drift or partial outages fail the run instead of silently publishing broken data.
MIN_COUNTS = {
    'vinnies': 350,
    'redcross': 130,
    'salvos': 230,
    'osm': 600,
    'total_kept': 1400,
}


def assert_quality(by_source, total_kept):
    problems = []
    for k, threshold in MIN_COUNTS.items():
        actual = total_kept if k == 'total_kept' else by_source.get(k, 0)
        if actual < threshold:
            problems.append(f'{k}: got {actual}, expected >= {threshold}')
    if problems:
        print('\nQUALITY GUARD FAILED — not rebuilding index.html:', file=sys.stderr)
        for p in problems:
            print(f'  - {p}', file=sys.stderr)
        sys.exit(1)


# ----- Build -----

def compact_records(shops):
    out = []
    for s in shops:
        rec = {'n': s['name'], 'y': s['lat'], 'x': s['lon'], 'src': s['source']}
        if s.get('operator'): rec['o'] = s['operator']
        if s.get('chain') and s['chain'] != 'independent': rec['c'] = s['chain']
        if s.get('address'): rec['a'] = s['address']
        if s.get('suburb'): rec['s'] = s['suburb']
        if s.get('state'): rec['st'] = s['state']
        if s.get('postcode'): rec['p'] = s['postcode']
        if s.get('phone'): rec['ph'] = s['phone']
        if s.get('hours'): rec['h'] = s['hours']
        out.append(rec)
    return out


def rebuild_html(compact):
    html = OUT_HTML.read_text(encoding='utf-8')
    data_json = json.dumps(compact, separators=(',', ':'), ensure_ascii=False)
    new_shops = f'const SHOPS = {data_json};'
    html2 = re.sub(r'const SHOPS = \[.*?\];', new_shops, html, count=1, flags=re.DOTALL)
    if html2 == html:
        raise RuntimeError('SHOPS substitution failed — sentinel not found in index.html')
    OUT_HTML.write_text(html2, encoding='utf-8')
    print(f'Wrote {OUT_HTML} ({len(html2)} bytes, embedded {len(data_json)/1024:.1f} KB)', flush=True)


def main():
    vinnies = scrape_vinnies()
    redcross = scrape_redcross()
    salvos_addrs = fetch_salvos_addresses()
    salvos = geocode_salvos(salvos_addrs, DATA / 'salvos_geocoded.json')
    osm = fetch_osm()

    all_shops = vinnies + redcross + salvos + osm
    print(f'Combined: {len(all_shops)}', flush=True)
    deduped = dedupe(all_shops)

    by_source = {}
    for s in deduped:
        by_source[s['source']] = by_source.get(s['source'], 0) + 1
    with_hours = sum(1 for s in deduped if s['hours'])
    print(f'Dedupe: {len(deduped)} kept, {len(all_shops)-len(deduped)} dropped', flush=True)
    print(f'By source: {by_source} · with hours: {with_hours}', flush=True)

    assert_quality(by_source, len(deduped))
    rebuild_html(compact_records(deduped))


if __name__ == '__main__':
    sys.exit(main() or 0)
