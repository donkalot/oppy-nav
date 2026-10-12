"""Refresh oppy-nav data: scrape chains + OSM, geocode Salvos, merge, rebuild index.html.

Designed to run in GitHub Actions weekly. Everything is idempotent — only commits when
the resulting HTML actually changes. Salvos geocoding is cached in data/salvos_geocoded.json
to avoid hammering Nominatim on every run.
"""
import datetime, json, math, pathlib, re, sys, time, urllib.parse, urllib.request
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

def vinnies_hours(opening_hours):
    """openingHours was {openingTimes: [{weekday, isScheduled, ...}]} until Sept 2026;
    it is now a flat list with renamed fields and HH:MM:SS times. Accept both."""
    times = opening_hours if isinstance(opening_hours, list) else ((opening_hours or {}).get('openingTimes') or [])
    hours = {}
    for t in times:
        wd = (t.get('name') or t.get('weekday') or '').lower()[:3]
        scheduled = t.get('scheduled', t.get('isScheduled'))
        if wd and scheduled and t.get('open') and t.get('close'):
            o, c = t['open'][:5], t['close'][:5]
            # A couple of shops carry a PM close tagged as AM ("09:00-05:00"). Showing
            # one as open at 3am is worse than showing no hours for that day.
            if c > o:
                hours[wd] = {'o': o, 'c': c}
    return hours


def parse_vinnies_page(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m: return None
    p = json.loads(m.group(1))['props']['pageProps']['pageData']
    coords = ((p.get('location') or {}).get('address') or {}).get('coordinates') or {}
    lat, lng = coords.get('lat'), coords.get('lng')
    if lat is None or lng is None: return None
    return {
        'name': p.get('shopName') or p.get('name') or 'Vinnies',
        'operator': 'Vinnies', 'chain': 'vinnies',
        'lat': round(float(lat), 6), 'lon': round(float(lng), 6),
        'address': p.get('addressLineOne', '') or '',
        'suburb': p.get('addressSuburb', '') or '',
        'state': p.get('addressState', '') or '',
        'postcode': p.get('addressPostcode', '') or '',
        'phone': p.get('phoneNumber', '') or '',
        'hours': vinnies_hours(p.get('openingHours')), 'source': 'vinnies',
    }


def scrape_vinnies():
    print('Vinnies: sitemap...', flush=True)
    txt = http_get('https://www.vinnies.org.au/sitemap.xml', timeout=30)
    urls = sorted(set(re.findall(r'https://[^<]+/shops/[^<]+', txt or '')))
    print(f'  {len(urls)} URLs', flush=True)

    def one(url):
        h = http_get(url)
        return parse_vinnies_page(h) if h else None

    return _parallel(urls, one, 'vinnies')


# ----- Red Cross -----

REDCROSS_DAYS = {'monday': 'mon', 'tuesday': 'tue', 'wednesday': 'wed',
                 'thursday': 'thu', 'friday': 'fri', 'saturday': 'sat',
                 'sunday': 'sun'}


def redcross_hours(entries):
    """Times arrive as bare 12-hour clock with no am/pm: a close of "5:00" is
    17:00. Opens are morning, closes are afternoon, so push any close before the
    open into the pm half."""
    hours = {}
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict) or e.get('closed'):
            continue
        wd = REDCROSS_DAYS.get(str(e.get('day', '')).strip().lower())
        o, c = str(e.get('opens') or ''), str(e.get('closes') or '')
        if not (wd and ':' in o and ':' in c):
            continue

        def hhmm(t, pm=False):
            hh, _, mm = t.partition(':')
            try: hh, mm = int(hh), int(mm)
            except ValueError: return None
            if pm and hh < 12: hh += 12
            if not (0 <= hh < 24 and 0 <= mm < 60): return None
            return f'{hh:02d}:{mm:02d}'

        oo = hhmm(o)
        cc = hhmm(c)
        if oo and cc and cc <= oo:
            cc = hhmm(c, pm=True)
        if oo and cc and cc > oo:
            hours[wd] = {'o': oo, 'c': cc}
    return hours


def parse_redcross_page(html):
    """Store details used to be JSON-LD. Since ~Oct 2026 they are an embedded JS
    props object with renamed keys (`locality`, not `addressLocality`), which also
    carries opening hours the JSON-LD never had."""
    i = html.find('"name":"LocationDetails"')
    if i < 0:
        return None
    # Start past the component's own "name" key, or grab() returns
    # "LocationDetails" as the shop name.
    i = html.index('"props"', i) + len('"props"')
    seg = html[i:i + 6000]

    def grab(key):
        m = re.search(r'"%s":"(.*?)(?<!\\)"' % key, seg)
        return (m.group(1).strip() if m else '')

    def coord(key, pat):
        m = re.search(r'"%s":\s*(%s)' % (key, pat), seg)
        return float(m.group(1)) if m else None

    lat, lon = coord('latitude', r'-?\d+\.\d+'), coord('longitude', r'-?\d+\.\d+')
    if lat is None or lon is None:
        return None
    hm = re.search(r'"operatingHours":\s*(\[.*?\])', seg, re.DOTALL)
    try:
        entries = json.loads(hm.group(1)) if hm else []
    except Exception:
        entries = []
    return {
        'name': grab('name') or 'Red Cross Shop',
        'operator': 'Red Cross', 'chain': 'redcross',
        'lat': round(lat, 6), 'lon': round(lon, 6),
        'address': grab('streetAddress'), 'suburb': grab('locality'),
        'state': grab('state'), 'postcode': grab('postalCode'),
        'phone': grab('phone'), 'hours': redcross_hours(entries),
    }


def scrape_redcross():
    print('Red Cross: sitemap...', flush=True)
    txt = http_get('https://www.redcross.org.au/sitemap.xml', timeout=30)
    urls = sorted(set(re.findall(r'https://[^<]+/retail-stores/[^<]+', txt or '')))
    print(f'  {len(urls)} URLs', flush=True)

    def one(url):
        h = http_get(url)
        if not h: return None
        d = parse_redcross_page(h)
        if not d: return None
        d['source'] = 'redcross'
        return d

    return _parallel(urls, one, 'redcross')


def _parallel(urls, fn, label, workers=10):
    out = []
    # Swallowing parse errors turned an upstream schema change into "0 valid" with no
    # cause in the log, so errors are counted and a sample is surfaced.
    errors = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, u): u for u in urls}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                r = f.result()
                if r: out.append(r)
            except Exception as e:
                errors.append(f'{futs[f]}: {type(e).__name__}: {e}')
            if i % 50 == 0 or i == len(urls):
                print(f'  {label}: {i}/{len(urls)} ({len(out)} valid, {len(errors)} errors)', flush=True)
    if errors:
        print(f'  {label}: {len(errors)} pages raised; first 3:', flush=True)
        for e in errors[:3]:
            print(f'    {e}', flush=True)
    return out


# ----- Salvos -----

SALVOS_UPLISTER_URL = 'https://www.salvosstores.com.au/api/uplister/store-list'

UPLISTER_DAYS = {'monday': 'mon', 'tuesday': 'tue', 'wednesday': 'wed',
                 'thursday': 'thu', 'friday': 'fri', 'saturday': 'sat',
                 'sunday': 'sun'}


def uplister_hours(opening_hours):
    """Per-day {'Opening': 'HH:MM:SS', 'Closing': ...}, or the string "Close".

    The Salvos GraphQL warehouse type carries no hours at all — introspection shows
    seven fields, none of them opening times — which is why this second endpoint is
    worth joining against.
    """
    hours = {}
    if not isinstance(opening_hours, dict):
        return hours
    for day, spec in opening_hours.items():
        wd = UPLISTER_DAYS.get(str(day).strip().lower())
        if not wd or not isinstance(spec, dict):
            continue
        o, c = str(spec.get('Opening') or ''), str(spec.get('Closing') or '')
        if not (re.fullmatch(r'\d{1,2}:\d{2}(:\d{2})?', o)
                and re.fullmatch(r'\d{1,2}:\d{2}(:\d{2})?', c)):
            continue
        o, c = o[:5], c[:5]
        # A zero-length day is a closed day expressed badly; publishing "9:00-9:00"
        # would read as open.
        if o == c:
            continue
        hours[wd] = {'o': o, 'c': c}
    return hours


def fetch_salvos_uplister():
    """Fetch the store-list feed behind the official locator, keyed by warehouse id.

    Returns {} on any failure: this is an enrichment pass, so a dead endpoint should
    cost hours and pin precision, not the 460-odd shops themselves.
    """
    print('Salvos: uplister store-list...', flush=True)
    try:
        r = requests.get(SALVOS_UPLISTER_URL, headers=HEADERS, timeout=60)
        if r.status_code != 200:
            print(f'  uplister -> {r.status_code}; skipping enrichment', flush=True)
            return {}
        data = r.json()
    except Exception as e:
        print(f'  uplister err: {e}; skipping enrichment', flush=True)
        return {}
    stores = list(data.values()) if isinstance(data, dict) else list(data or [])
    out = {}
    for s in stores:
        if not isinstance(s, dict) or not s.get('SaleorId'):
            continue
        out[s['SaleorId']] = {
            'hours': uplister_hours(s.get('OpeningHours')),
            'lat': _num(s.get('Latitude')),
            'lon': _num(s.get('Longitude')),
            # Salvos' own locator renders neither: its store list is filtered with
            # `0 === HiddenStore && 0 === isPermanentlyClosed`. Mapping a shop that
            # shut in 2024 is worse than omitting it.
            'closed': bool(s.get('isPermanentlyClosed') or s.get('isClosed')
                           or s.get('HiddenStore')),
        }
    closed = sum(1 for v in out.values() if v['closed'])
    print(f'  {len(stores)} stores, {len(out)} with ids, {closed} flagged closed',
          flush=True)
    return out


def _num(x):
    """Coords arrive as strings and some carry a trailing comma ("-38.356169,")."""
    try:
        return float(str(x).strip().rstrip(',').strip())
    except (TypeError, ValueError):
        return None


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
    seen_cursors = set()
    while True:
        req = urllib.request.Request(url, data=json.dumps({'query': query, 'variables': {'first': 100, 'after': after}}).encode(), headers={'Content-Type':'application/json','User-Agent':UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        # A GraphQL schema change returns 200 with data:null and an errors array, which
        # would otherwise surface as an opaque TypeError on the next subscript.
        if d.get('errors'):
            raise RuntimeError(f'Salvos GraphQL errors: {json.dumps(d["errors"])[:500]}')
        conn = ((d.get('data') or {}).get('warehouses'))
        if conn is None:
            raise RuntimeError(f'Salvos GraphQL: no warehouses in response; keys={list(d.keys())}')
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
        # A cursor that stops advancing would otherwise page forever until the job
        # hits its 45-minute wall with no explanation.
        if after in seen_cursors:
            raise RuntimeError(f'Salvos GraphQL: cursor {after!r} repeated — pagination stuck')
        seen_cursors.add(after)
    print(f'  {len(out)} salvos records', flush=True)
    return out


SALVOS_TEST_RECORD = re.compile(r'(?i)\btest\b')

SALVOS_UNIT_PREFIX = re.compile(
    r'(?i)^\s*(?:shop|unit|suite|tenancy|tnc|level|kiosk|t)\s*[\w&\-/. ]{0,12}?[,/]\s*')
SALVOS_BARE_UNIT = re.compile(r'^\s*[\dA-Za-z&\- ]{1,10}/\s*')


def salvos_street_variants(street):
    """Nominatim can't resolve a shopping-centre designator: "Shop 1, 60 Mill Street"
    and "T3-5 / 322 Hale Road" both return nothing while the bare street number
    resolves fine. Yield the original first, then progressively stripped forms."""
    seen = []
    for s in (street or '',
              SALVOS_UNIT_PREFIX.sub('', street or ''),
              SALVOS_BARE_UNIT.sub('', SALVOS_UNIT_PREFIX.sub('', street or ''))):
        s = s.strip()
        if s and s not in seen:
            seen.append(s)
    return seen


def geocode_salvos(records, cache_path, uplister=None):
    """Only geocode records whose id isn't already in the cache."""
    cache = {}
    if cache_path.exists():
        for r in json.loads(cache_path.read_text(encoding='utf-8')):
            cache[r['id']] = r
    up = uplister or {}
    # Uplister ships real store coordinates, so anything it covers needs no Nominatim
    # call at all — and its coords are better than ours even when we already have some.
    todo = [s for s in records
            if (s['id'] not in cache or 'lat' not in cache[s['id']])
            and (up.get(s['id']) or {}).get('lat') is None]
    print(f'Salvos geocode: cached={len([r for r in cache.values() if "lat" in r])}, '
          f'to-geocode={len(todo)}', flush=True)

    def geocode(q):
        u = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode({
            'q': q, 'format': 'json', 'limit': 1, 'countrycodes': 'au'})
        req = urllib.request.Request(u, headers={'User-Agent': UA, 'Accept-Language': 'en-AU'})
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.load(r)
        if not j: return None
        return float(j[0]['lat']), float(j[0]['lon'])

    def attempt(q):
        for _ in range(3):
            try:
                return geocode(q)
            except Exception:
                time.sleep(2)
        return None

    t0 = time.time()
    approx = 0
    for i, s in enumerate(todo, 1):
        tail = [p for p in (s['city'], s['state'], s['postcode']) if p]
        coords, precision = None, None
        for street in salvos_street_variants(s['street']):
            coords = attempt(', '.join([street] + tail) + ', Australia')
            if coords:
                precision = 'street'
                break
            time.sleep(1.05)
        if not coords and tail:
            # Suburb-level coords put the pin a few hundred metres out, which the app
            # already tolerates for Salvos (dedupe uses a wider radius for this source
            # precisely because every coord here is geocoded). Dropping the shop
            # instead makes it invisible, which is strictly worse for a shop finder.
            coords = attempt(', '.join(tail) + ', Australia')
            if coords:
                precision, approx = 'suburb', approx + 1
        rec = dict(cache.get(s['id'], s))
        rec.update(s)
        if coords:
            rec['lat'], rec['lon'] = coords
            rec['precision'] = precision
        cache[s['id']] = rec
        if i % 25 == 0 or i == len(todo):
            cache_path.write_text(json.dumps(list(cache.values()), ensure_ascii=False), encoding='utf-8')
            print(f'  {i}/{len(todo)} · {sum(1 for r in cache.values() if "lat" in r)} coords total '
                  f'· {approx} suburb-level · {i/(time.time()-t0):.2f}/s', flush=True)
        time.sleep(1.05)

    # Drop cached ids no longer in the current warehouse list
    current_ids = {s['id'] for s in records}
    for stale_id in list(cache.keys()):
        if stale_id not in current_ids:
            del cache[stale_id]
    cache_path.write_text(json.dumps(list(cache.values()), ensure_ascii=False), encoding='utf-8')

    out = []
    dropped_closed = dropped_test = exact = with_hours = 0
    for r in cache.values():
        u = up.get(r['id']) or {}
        if u.get('closed'):
            dropped_closed += 1
            continue
        # The warehouse list carries two literal fixtures ("Test Warehouse Only" at
        # "TEST CITY", "Test Geps, SA" on "88 Test Street Street"). Uplister omits
        # both, so they survive only because they have no uplister record to flag.
        if SALVOS_TEST_RECORD.search(r['name']) or SALVOS_TEST_RECORD.search(r.get('street') or ''):
            dropped_test += 1
            continue
        lat, lon = u.get('lat'), u.get('lon')
        approx = r.get('precision') == 'suburb'
        if lat is not None and lon is not None:
            # Two geocoded pins sat 109km and 100km from the real shop, which the
            # "approximate" badge understates badly.
            if approx:
                exact += 1
            approx = False
        elif 'lat' in r:
            lat, lon = r['lat'], r['lon']
        else:
            continue
        hours = u.get('hours') or {}
        with_hours += 1 if hours else 0
        out.append({
            'name': r['name'], 'operator': 'Salvos', 'chain': 'salvos',
            'lat': round(float(lat), 6), 'lon': round(float(lon), 6),
            'address': r['street'], 'suburb': r['city'], 'state': r['state'],
            'postcode': str(r['postcode']), 'phone': r['phone'],
            'hours': hours, 'source': 'salvos',
            'approx': approx,
        })
    print(f'Salvos: {len(out)} of {len(records)} upstream records placed '
          f'({sum(1 for r in out if r["approx"])} suburb-level, {with_hours} with hours); '
          f'dropped {dropped_closed} closed, {dropped_test} test; '
          f'{exact} pins upgraded to exact', flush=True)
    return out


# ----- OSM -----

OSM_DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
OSM_DAY_INDEX = {'mo': 0, 'tu': 1, 'we': 2, 'th': 3, 'fr': 4, 'sa': 5, 'su': 6}


def parse_osm_hours(spec):
    """Parse the subset of OSM opening_hours syntax that AU charity shops actually use.

    Returns the same {'mon': {'o','c'}} shape the app consumes. Anything with no
    plain weekday/time rule — 24/7, month ranges, PH-only, sunrise — yields {} so a
    shop is simply treated as having unknown hours rather than wrong ones.
    """
    if not spec:
        return {}
    hours = {}
    # ';' always separates rules. ',' is overloaded: a day list ("Mo,Tu 10:00-14:00")
    # or a rule separator ("We-Fr 09:30-16:00, Sa 09:30-14:30"). Only split on a comma
    # that is followed by something introducing a fresh day spec plus its own time.
    # A day list is bare days ("Mo,Tu,Th") with no time between them, so only split
    # where the text before the next comma already contains a time span.
    chunks = []
    for part in spec.split(';'):
        chunks.extend(re.split(r'(?<=\d:\d\d),(?=\s*(?:Mo|Tu|We|Th|Fr|Sa|Su|PH|SH)\b)', part, flags=re.I))
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        # Holiday rules describe exceptions, not a weekly pattern.
        if re.search(r'\b(PH|SH)\b', chunk):
            continue
        # The day spec may contain spaces after commas, e.g. "Mo-Fr, Su 09:00-17:00".
        m = re.match(r'^((?:[A-Za-z]{2}(?:\s*-\s*[A-Za-z]{2})?)(?:\s*,\s*[A-Za-z]{2}(?:\s*-\s*[A-Za-z]{2})?)*)\s*(.*)$', chunk)
        if not m:
            continue
        dayspec, rest = m.group(1), m.group(2).strip()
        days = []
        for token in dayspec.rstrip(',').split(','):
            token = token.strip().lower()
            rng = re.match(r'^([a-z]{2})\s*-\s*([a-z]{2})$', token)
            if rng and rng.group(1) in OSM_DAY_INDEX and rng.group(2) in OSM_DAY_INDEX:
                a, b = OSM_DAY_INDEX[rng.group(1)], OSM_DAY_INDEX[rng.group(2)]
                # Ranges may wrap, e.g. Sa-Su or Fr-Mo.
                days.extend(OSM_DAYS[a:b + 1] if a <= b else OSM_DAYS[a:] + OSM_DAYS[:b + 1])
            elif token[:2] in OSM_DAY_INDEX and len(token) <= 2:
                days.append(OSM_DAYS[OSM_DAY_INDEX[token[:2]]])
        if not days:
            continue
        # "off"/"closed" means explicitly shut; drop any span claimed earlier.
        if re.match(r'^(off|closed)$', rest, re.I):
            for d in days:
                hours.pop(d, None)
            continue
        t = re.match(r'^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})', rest)
        if not t:
            continue
        o = f'{int(t.group(1)):02d}:{t.group(2)}'
        c = f'{int(t.group(3)):02d}:{t.group(4)}'
        # A close at or before the open means a mis-tagged PM time upstream (a few AU
        # shops carry "09:00-04:45"). Showing such a shop as open at 2am is worse than
        # showing no hours, so skip it.
        if c <= o:
            continue
        for d in days:
            hours[d] = {'o': o, 'c': c}
    return hours


# 1078 charity shops at 2026-10-07. Any mirror answering with less than a third of
# that is broken or throttling, not reporting a real collapse in the OSM data.
OSM_MIN_ELEMENTS = 350
# Mirrors fall months behind without ever erroring: kumi.systems served a 2026-05-06
# database on 2026-10-07, private.coffee a 2026-05-31 one on 2026-10-12. Both answered
# 200 with a plausible element count, so only the replication timestamp reveals it.
OSM_MAX_DB_AGE_DAYS = 14


def osm_db_age_days(data, now=None):
    """Days between the mirror's OSM replication timestamp and now, or None if it
    didn't report one."""
    ts = (data.get('osm3s') or {}).get('timestamp_osm_base')
    if not ts:
        return None
    try:
        base = datetime.datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    return (now - base).total_seconds() / 86400


OSM_ENDPOINTS = [
    # overpass-api.de first: kumi.systems was serving a five-month-stale database on
    # 2026-10-07 (base 2026-05-06) and answering AU-wide queries with zero elements.
    'https://overpass-api.de/api/interpreter',
    'https://overpass.kumi.systems/api/interpreter',
    'https://overpass.private.coffee/api/interpreter',
]


def osm_verdict(data, now=None):
    """Classify an Overpass 200 body as 'ok', 'stale', or 'thin'.

    Returned alongside a human-readable reason. Separated from the fetch so the
    accept/reject rules are testable without network access.
    """
    n = len(data.get('elements', []))
    base = (data.get('osm3s') or {}).get('timestamp_osm_base')
    age = osm_db_age_days(data, now=now)
    aged = 'age unknown' if age is None else f'{age:.1f}d old'
    # A mirror can answer 200 with an empty result set: either it is rate-limiting us
    # (the reason arrives in "remark", not the status code) or its database is months
    # stale. Taking that as success silently drops every OSM shop.
    if n < OSM_MIN_ELEMENTS:
        return 'thin', f'only {n} elements (base {base}, remark {data.get("remark")!r})'
    # A stale mirror returns a plausible count, so the count alone can't catch it.
    # Publishing it quietly reverts months of OSM edits.
    if age is not None and age > OSM_MAX_DB_AGE_DAYS:
        return 'stale', f'{n} elements but database is {age:.0f} days stale (base {base})'
    return 'ok', f'{n} elements, base {base}, {aged}'


def fetch_osm(rounds=2, backoff=90):
    """Fetch AU charity shops, trying each mirror until one answers fresh and full.

    overpass-api.de 504s intermittently under load, which would otherwise fail the
    whole weekly run now that a stale fallback is no longer accepted. A mirror that
    is *stale* will not become fresh in a minute, so only transient failures —
    non-200, network errors — are worth a second pass.
    """
    print('OSM: overpass...', flush=True)
    q = '[out:json][timeout:120];area["ISO3166-1"="AU"][admin_level=2]->.au;nwr["shop"="charity"](area.au);out center tags;'
    candidates = list(OSM_ENDPOINTS)
    for attempt in range(1, rounds + 1):
        retryable = []
        for ep in candidates:
            try:
                r = requests.post(ep, data={'data': q}, headers=HEADERS, timeout=180)
                if r.status_code != 200:
                    print(f'  {ep} -> {r.status_code}', flush=True)
                    retryable.append(ep)
                    continue
                data = r.json()
                verdict, why = osm_verdict(data)
                if verdict == 'ok':
                    print(f'  fetched from {ep} ({why})', flush=True)
                    return parse_osm_elements(data)
                print(f'  {ep} -> 200 but {why} — trying next mirror', flush=True)
                # 'thin' can be transient throttling; 'stale' is a property of the
                # mirror and won't change between rounds.
                if verdict == 'thin':
                    retryable.append(ep)
            except Exception as e:
                print(f'  {ep} err: {e}', flush=True)
                retryable.append(ep)
        candidates = retryable
        if not candidates or attempt == rounds:
            break
        print(f'  all mirrors unusable; retrying {len(candidates)} in {backoff}s '
              f'(round {attempt + 1} of {rounds})', flush=True)
        time.sleep(backoff)
    raise RuntimeError(
        'No Overpass mirror returned a fresh, populated result — all were '
        'unreachable, too small, or too stale. Retry later rather than '
        'publishing: a partial OSM result silently deletes shops.')


def parse_osm_elements(data):
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
            'address': ' '.join(v for v in (tags.get('addr:housenumber'),
                                            tags.get('addr:street')) if v),
            'suburb': tags.get('addr:suburb') or tags.get('addr:city', ''),
            'state': tags.get('addr:state', ''),
            'postcode': tags.get('addr:postcode', ''), 'phone': tags.get('phone', ''),
            'hours': parse_osm_hours(tags.get('opening_hours')), 'source': 'osm',
        })
    print(f'OSM: {len(shops)} shops', flush=True)
    return shops


# ----- Reverse geocoding -----

# Nominatim returns full state names; every other source emits the abbreviation, and
# the app's state filter matches on it.
AU_STATES = {
    'new south wales': 'NSW', 'victoria': 'VIC', 'queensland': 'QLD',
    'south australia': 'SA', 'western australia': 'WA', 'tasmania': 'TAS',
    'australian capital territory': 'ACT', 'northern territory': 'NT',
}


def reverse_state(address):
    """Pull a state abbreviation out of a Nominatim address block.

    The ISO code is the reliable field: the ACT arrives under `territory` rather than
    `state`, so reading `state` alone silently blanks every Canberra shop.
    """
    iso = (address.get('ISO3166-2-lvl4') or '')
    if iso.startswith('AU-'):
        return iso[3:]
    for field in ('state', 'territory'):
        hit = AU_STATES.get((address.get(field) or '').lower())
        if hit:
            return hit
    return ''

def reverse_geocode_missing(shops, cache_path, limit=None):
    """Fill in addresses for shops that have coordinates but no address.

    These are OSM records with no `addr:*` tags at all — genuinely absent upstream,
    so no parser change reaches them. Nominatim resolves them backwards from the
    coordinates instead. Cached by rounded coordinate so the weekly run only pays for
    newly-appeared shops.

    Addresses added here are marked `address_approx` and are deliberately NOT counted
    towards the with_address quality floor: a backfill that counted would mask exactly
    the upstream parse breaks that floor exists to catch.
    """
    cache = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding='utf-8'))

    def key(s):
        return f"{s['lat']:.5f},{s['lon']:.5f}"

    todo = [s for s in shops
            if not s.get('address') and not s.get('suburb')
            and s.get('lat') is not None and key(s) not in cache]
    print(f'Reverse geocode: cached={len(cache)}, to-look-up={len(todo)}', flush=True)
    if limit is not None:
        todo = todo[:limit]

    t0 = time.time()
    for i, s in enumerate(todo, 1):
        u = 'https://nominatim.openstreetmap.org/reverse?' + urllib.parse.urlencode({
            'lat': s['lat'], 'lon': s['lon'], 'format': 'json', 'zoom': 18,
            'addressdetails': 1})
        a = None
        for _ in range(3):
            try:
                req = urllib.request.Request(
                    u, headers={'User-Agent': UA, 'Accept-Language': 'en-AU'})
                with urllib.request.urlopen(req, timeout=20) as r:
                    a = (json.load(r) or {}).get('address') or {}
                break
            except Exception:
                time.sleep(2)
        if a is None:
            continue
        road = a.get('road') or ''
        cache[key(s)] = {
            'address': ' '.join(v for v in (a.get('house_number'), road) if v),
            'suburb': a.get('suburb') or a.get('city') or a.get('town')
                      or a.get('village') or a.get('municipality') or '',
            'state': reverse_state(a),
            'postcode': a.get('postcode') or '',
        }
        if i % 25 == 0 or i == len(todo):
            cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
            print(f'  {i}/{len(todo)} · {i/max(time.time()-t0, 0.001):.2f}/s', flush=True)
        time.sleep(1.1)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')

    filled = 0
    for s in shops:
        if s.get('address') or s.get('suburb') or s.get('lat') is None:
            continue
        hit = cache.get(key(s))
        if not hit or not (hit.get('address') or hit.get('suburb')):
            continue
        for field in ('address', 'suburb', 'state', 'postcode'):
            if hit.get(field) and not s.get(field):
                s[field] = hit[field]
        s['address_approx'] = True
        filled += 1
    print(f'Reverse geocode: filled {filled} of '
          f'{sum(1 for s in shops if not s.get("address") and not s.get("suburb")) + filled}',
          flush=True)
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
        match = None
        for dy in (-2,-1,0,1,2):
            for dx in (-2,-1,0,1,2):
                for k in buckets.get((bk[0]+dy, bk[1]+dx), []):
                    d = dist_km(k, s)
                    radius = 0.30 if (s['source'] == 'salvos' or k['source'] == 'salvos') else 0.15
                    if d < radius:
                        if s['chain'] == k['chain'] and s['chain'] != 'independent':
                            match = k; break
                        if s['source'] == 'osm' and k['source'] in ('vinnies','redcross','salvos'):
                            hay = (s['name'] + ' ' + s['operator']).lower()
                            if k['chain'] == 'vinnies' and ('vinn' in hay or 'vincent' in hay): match = k; break
                            if k['chain'] == 'redcross' and 'red cross' in hay: match = k; break
                            if k['chain'] == 'salvos' and ('salv' in hay or 'salvation' in hay): match = k; break
                if match: break
            if match: break
        if match:
            # A dropped OSM twin is sometimes the only record that knows when the shop
            # is open. Keep the winner's identity, inherit the detail it lacks.
            if s.get('hours') and not match.get('hours'):
                match['hours'] = s['hours']
            for field in ('phone', 'postcode', 'state'):
                if s.get(field) and not match.get(field):
                    match[field] = s[field]
            continue
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
    # 439 at 2026-10-12: the three-tier geocode recovered 154 records the single-tier
    # version silently dropped, then 29 permanently-closed and test records were
    # suppressed. Floor catches a regression to street-only geocoding (317).
    'salvos': 400,
    'osm': 600,
    'total_kept': 1500,
    # Counts alone can't catch a break in the hours sub-schema: coords still parse,
    # every threshold passes, and the app ships with a dead "Open now" filter.
    # 1214 at 2026-10-12 (vinnies 447, salvos 408, osm 190, redcross 169) — Salvos went
    # 31 -> 408 once the uplister feed was joined in, since the GraphQL warehouse type
    # carries no hours at all. Set so a break in either large source's hours sub-schema
    # trips it (vinnies -> 767, salvos -> 806) while leaving 18% for OSM tagging churn.
    'with_hours': 1000,
    # Two defects hid behind the shop counts for months: Red Cross moved its store
    # details out of JSON-LD, and scrape_osm hardcoded 'address' to empty. Both left
    # the coords intact, so 695 of 1733 shops shipped a popup containing nothing but
    # a name while every count above passed. 1365 at 2026-10-07; losing the Red Cross
    # blob drops it to ~1187 and losing the OSM tags to ~1093, so sit above both while
    # leaving room for ordinary OSM tagging churn.
    'with_address': 1250,
}


def assert_quality(by_source, total_kept, with_hours=None, with_address=None):
    counts = dict(by_source)
    counts['total_kept'] = total_kept
    optional = {'with_hours': with_hours, 'with_address': with_address}
    counts.update({k: v for k, v in optional.items() if v is not None})
    problems = []
    for k, threshold in MIN_COUNTS.items():
        if k in optional and optional[k] is None:
            continue
        actual = counts.get(k, 0)
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
        if s.get('approx'): rec['ax'] = 1
        # Address derived from the coords rather than published by the shop: the street
        # is whatever Nominatim finds at that point, so it can name the building next
        # door. Worth showing, worth labelling.
        if s.get('address_approx'): rec['aa'] = 1
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
    salvos = geocode_salvos(salvos_addrs, DATA / 'salvos_geocoded.json',
                            fetch_salvos_uplister())
    osm = fetch_osm()

    all_shops = vinnies + redcross + salvos + osm
    print(f'Combined: {len(all_shops)}', flush=True)
    deduped = dedupe(all_shops)

    by_source = {}
    for s in deduped:
        by_source[s['source']] = by_source.get(s['source'], 0) + 1
    with_hours = sum(1 for s in deduped if s['hours'])
    with_address = sum(1 for s in deduped if s.get('address'))
    print(f'Dedupe: {len(deduped)} kept, {len(all_shops)-len(deduped)} dropped', flush=True)
    print(f'By source: {by_source} · with hours: {with_hours} '
          f'· with address: {with_address}', flush=True)

    # Guard on upstream-parsed addresses only, before the backfill: counting
    # reverse-geocoded ones would hold the number up while a source's address parsing
    # broke, which is the failure the floor exists to catch.
    assert_quality(by_source, len(deduped), with_hours, with_address)

    # After dedupe, so we never spend a lookup on a record that gets dropped.
    deduped = reverse_geocode_missing(deduped, DATA / 'reverse_geocoded.json')
    rebuild_html(compact_records(deduped))


if __name__ == '__main__':
    sys.exit(main() or 0)
