"""Unit tests for pure helpers in scrape_all.py.

Run: pytest scripts/test_scrape.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import json

import pytest

import scrape_all
from scrape_all import (
    dist_km, dedupe, compact_records, assert_quality, MIN_COUNTS,
    vinnies_hours, parse_vinnies_page, parse_osm_hours,
    redcross_hours, parse_redcross_page, salvos_street_variants,
    osm_db_age_days, OSM_MAX_DB_AGE_DAYS, osm_verdict, OSM_MIN_ELEMENTS,
    uplister_hours, SALVOS_TEST_RECORD,
)
import datetime


# ----- uplister_hours -----
# The Salvos GraphQL type has no hours field at all, which is why only 9% of Salvos
# shops had hours before this feed was joined in.

def test_uplister_hours_parses_a_normal_week():
    got = uplister_hours({
        'Monday': {'Opening': '09:00:00', 'Closing': '17:00:00'},
        'Saturday': {'Opening': '09:00:00', 'Closing': '17:30:00'},
        'Sunday': 'Close',
    })
    assert got == {'mon': {'o': '09:00', 'c': '17:00'},
                   'sat': {'o': '09:00', 'c': '17:30'}}


def test_uplister_hours_drops_closed_days():
    """A closed day arrives as the bare string "Close", not a dict."""
    assert uplister_hours({'Sunday': 'Close'}) == {}


def test_uplister_hours_drops_a_zero_length_day():
    """Open == close is a closed day expressed badly; emitting it would render as
    "9:00-9:00" and read as open."""
    assert uplister_hours({'Monday': {'Opening': '09:00:00', 'Closing': '09:00:00'}}) == {}


def test_uplister_hours_ignores_junk_times():
    assert uplister_hours({'Monday': {'Opening': '', 'Closing': '17:00:00'}}) == {}
    assert uplister_hours({'Monday': {'Opening': 'nine', 'Closing': '17:00:00'}}) == {}
    assert uplister_hours({'Monday': {}}) == {}


def test_uplister_hours_tolerates_a_missing_block():
    """Enrichment must never raise: a shape change here should cost hours, not the
    460-odd Salvos shops."""
    assert uplister_hours(None) == {}
    assert uplister_hours([]) == {}
    assert uplister_hours({'Funday': {'Opening': '09:00:00', 'Closing': '17:00:00'}}) == {}


def test_salvos_test_record_matches_only_the_fixtures():
    """Checked against the live roster: these two match, and no real store name or
    street in either feed does."""
    assert SALVOS_TEST_RECORD.search('Test Warehouse Only')
    assert SALVOS_TEST_RECORD.search('88 Test Street Street')
    for real in ('Youngtown, TAS', 'Belconnen', 'Testament Road', 'Protestant Hill',
                 '405-407 Hobart Road'):
        assert not SALVOS_TEST_RECORD.search(real), real


# ----- osm_db_age_days -----
# Mirrors fall months behind while still answering 200 with a plausible element
# count. On 2026-10-12 private.coffee served a 2026-05-31 database and shipped 719
# OSM shops instead of 763 before this check existed.

NOW = datetime.datetime(2026, 10, 12, 12, 0, 0)


def _d(ts):
    return {'osm3s': {'timestamp_osm_base': ts}}


def test_osm_db_age_fresh():
    age = osm_db_age_days(_d('2026-10-12T02:40:45Z'), now=NOW)
    assert 0 <= age < 1


def test_osm_db_age_detects_the_stale_mirror():
    age = osm_db_age_days(_d('2026-05-31T22:37:44Z'), now=NOW)
    assert age > OSM_MAX_DB_AGE_DAYS


def test_osm_db_age_threshold_accepts_a_normal_lag():
    """Healthy mirrors run hours behind, not days. A week of lag must still pass so a
    transient replication hiccup doesn't fail the weekly run."""
    assert osm_db_age_days(_d('2026-10-05T12:00:00Z'), now=NOW) < OSM_MAX_DB_AGE_DAYS


def test_osm_db_age_missing_or_unparseable_is_none():
    """Unknown age must not be treated as zero — that would wave a stale mirror
    through on a malformed timestamp."""
    assert osm_db_age_days({}, now=NOW) is None
    assert osm_db_age_days(_d('not-a-date'), now=NOW) is None
    assert osm_db_age_days({'osm3s': {}}, now=NOW) is None


# ----- osm_verdict -----
# The verdict decides two things: whether to publish, and whether a second pass at
# the same mirror is worth the wait. 'thin' can be throttling that clears; 'stale'
# is a property of the mirror's database and will not change in 90 seconds.

def _body(n, ts, **extra):
    return {'elements': [{'type': 'node'}] * n,
            'osm3s': {'timestamp_osm_base': ts}, **extra}


def test_osm_verdict_accepts_a_healthy_mirror():
    verdict, _ = osm_verdict(_body(1151, '2026-10-12T02:40:45Z'), now=NOW)
    assert verdict == 'ok'


def test_osm_verdict_calls_the_stale_but_full_mirror_stale():
    """private.coffee's 2026-10-12 answer: 1079 elements — well over the floor — from a
    2026-05-31 database. Volume alone passes it, which is how it shipped."""
    verdict, _ = osm_verdict(_body(1079, '2026-05-31T22:37:44Z'), now=NOW)
    assert verdict == 'stale'


def test_osm_verdict_calls_an_empty_result_thin_not_ok():
    """kumi.systems' 2026-10-07 answer: HTTP 200, zero elements, remark set."""
    verdict, _ = osm_verdict(
        _body(0, '2026-05-06T00:00:00Z', remark='runtime error: Query timed out'),
        now=NOW)
    assert verdict == 'thin'


def test_osm_verdict_thin_wins_over_stale():
    """Classification order matters for the retry: a mirror that is both empty and stale
    must read 'thin' so the second pass still revisits it — the emptiness may be
    throttling, and a later round could get real data from somewhere."""
    verdict, _ = osm_verdict(_body(OSM_MIN_ELEMENTS - 1, '2026-01-01T00:00:00Z'),
                             now=NOW)
    assert verdict == 'thin'


def test_osm_verdict_unknown_age_is_not_treated_as_stale():
    """A mirror that omits osm3s shouldn't be rejected on age it never claimed; the
    element floor is the only check that can apply."""
    verdict, _ = osm_verdict({'elements': [{'type': 'node'}] * 1151}, now=NOW)
    assert verdict == 'ok'


def test_osm_verdict_reason_names_the_numbers():
    """The reason string is the only diagnostic in the cron log, so it has to carry the
    element count and the database timestamp — the 2026-09 Vinnies break went four
    weeks unnoticed partly because the log said nothing useful."""
    _, why = osm_verdict(_body(1079, '2026-05-31T22:37:44Z'), now=NOW)
    assert '1079' in why and '2026-05-31' in why


# ----- fetch_osm retry -----
# Rejecting stale mirrors means a transient 504 on the healthy one now fails the whole
# weekly run. These cover which failures earn a second pass and which don't.

FRESH = '2026-10-12T02:40:45Z'


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError('not json')
        return self._body


def _fake_requests(monkeypatch, responses):
    """Serve a scripted response per (endpoint, visit-number) and record call order."""
    calls = []

    def post(url, **kw):
        seen = sum(1 for c in calls if c == url)
        calls.append(url)
        r = responses[url][min(seen, len(responses[url]) - 1)]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(scrape_all.requests, 'post', post)
    monkeypatch.setattr(scrape_all.time, 'sleep', lambda s: None)
    monkeypatch.setattr(scrape_all, 'parse_osm_elements', lambda d: d['elements'])
    return calls


def test_fetch_osm_retries_a_transient_504_and_succeeds():
    """The real 2026-10-12 failure: overpass-api.de 504s, the fallbacks are stale. One
    round would raise; a second pass finds the primary recovered."""
    de, kumi, coffee = scrape_all.OSM_ENDPOINTS
    with pytest.MonkeyPatch.context() as mp:
        calls = _fake_requests(mp, {
            de: [_Resp(504), _Resp(200, _body(1151, FRESH))],
            kumi: [_Resp(200, _body(1079, '2026-05-31T22:37:44Z'))],
            coffee: [_Resp(200, _body(1079, '2026-05-31T22:37:44Z'))],
        })
        assert len(scrape_all.fetch_osm(rounds=2, backoff=0)) == 1151
    assert calls == [de, kumi, coffee, de]


def test_fetch_osm_does_not_revisit_a_stale_mirror():
    """A stale database won't replicate five months in 90 seconds, so re-querying it
    only burns the backoff. Only the transient failure is worth a second look."""
    de, kumi, coffee = scrape_all.OSM_ENDPOINTS
    stale = _Resp(200, _body(1079, '2026-05-31T22:37:44Z'))
    with pytest.MonkeyPatch.context() as mp:
        calls = _fake_requests(mp, {
            de: [stale, stale],
            kumi: [_Resp(500), _Resp(200, _body(1151, FRESH))],
            coffee: [stale, stale],
        })
        assert len(scrape_all.fetch_osm(rounds=2, backoff=0)) == 1151
    assert calls.count(de) == 1 and calls.count(coffee) == 1


def test_fetch_osm_raises_rather_than_publishing_a_stale_result():
    """Failing the run is the correct outcome: a partial OSM result deletes shops from
    the published map, and the quality guard downstream can't tell the difference
    between 'mirror was stale' and 'these shops closed'."""
    stale = _Resp(200, _body(1079, '2026-05-31T22:37:44Z'))
    with pytest.MonkeyPatch.context() as mp:
        _fake_requests(mp, {ep: [stale, stale] for ep in scrape_all.OSM_ENDPOINTS})
        with pytest.raises(RuntimeError, match='stale'):
            scrape_all.fetch_osm(rounds=2, backoff=0)


def test_fetch_osm_stops_early_when_nothing_is_retryable():
    """All-stale means no candidates remain, so the second round must not sleep through
    a backoff it cannot use — the cron has a finite budget."""
    stale = _Resp(200, _body(1079, '2026-05-31T22:37:44Z'))
    slept = []
    with pytest.MonkeyPatch.context() as mp:
        _fake_requests(mp, {ep: [stale] for ep in scrape_all.OSM_ENDPOINTS})
        mp.setattr(scrape_all.time, 'sleep', lambda s: slept.append(s))
        with pytest.raises(RuntimeError):
            scrape_all.fetch_osm(rounds=3, backoff=90)
    assert slept == []


def test_fetch_osm_survives_a_network_error():
    """An exception mid-loop must not abandon the remaining mirrors."""
    de, kumi, _ = scrape_all.OSM_ENDPOINTS
    with pytest.MonkeyPatch.context() as mp:
        _fake_requests(mp, {
            de: [OSError('connection reset')],
            kumi: [_Resp(200, _body(1151, FRESH))],
            scrape_all.OSM_ENDPOINTS[2]: [_Resp(500)],
        })
        assert len(scrape_all.fetch_osm(rounds=1, backoff=0)) == 1151


# ----- parse_osm_hours -----

def _spans(spec):
    return {d: f"{v['o']}-{v['c']}" for d, v in parse_osm_hours(spec).items()}


def test_osm_hours_simple_range():
    assert _spans('Mo-Fr 09:00-17:00') == {
        'mon': '09:00-17:00', 'tue': '09:00-17:00', 'wed': '09:00-17:00',
        'thu': '09:00-17:00', 'fri': '09:00-17:00'}


def test_osm_hours_multiple_rules():
    assert _spans('Mo-Fr 09:00-16:00; Sa 09:30-13:30') == {
        'mon': '09:00-16:00', 'tue': '09:00-16:00', 'wed': '09:00-16:00',
        'thu': '09:00-16:00', 'fri': '09:00-16:00', 'sat': '09:30-13:30'}


def test_osm_hours_comma_is_day_list_not_rule_separator():
    """'Mo,Tu,Th,Fr 10:00-14:00' is one rule over four days — not a rule boundary."""
    assert _spans('Mo,Tu,Th,Fr 10:00-14:00') == {
        'mon': '10:00-14:00', 'tue': '10:00-14:00',
        'thu': '10:00-14:00', 'fri': '10:00-14:00'}


def test_osm_hours_comma_separating_rules():
    assert _spans('We-Fr 09:30-16:00, Sa 09:30-14:30') == {
        'wed': '09:30-16:00', 'thu': '09:30-16:00',
        'fri': '09:30-16:00', 'sat': '09:30-14:30'}


def test_osm_hours_day_list_with_space():
    assert _spans('Mo-Fr, Su 09:00-17:00') == {
        'mon': '09:00-17:00', 'tue': '09:00-17:00', 'wed': '09:00-17:00',
        'thu': '09:00-17:00', 'fri': '09:00-17:00', 'sun': '09:00-17:00'}


def test_osm_hours_off_and_closed_are_dropped():
    assert 'sun' not in parse_osm_hours('Mo-Sa 09:00-16:50; Su closed')
    assert _spans('Mo-Fr 10:00-16:00; Sa-Su off') == {
        'mon': '10:00-16:00', 'tue': '10:00-16:00', 'wed': '10:00-16:00',
        'thu': '10:00-16:00', 'fri': '10:00-16:00'}


def test_osm_hours_off_overrides_earlier_span():
    """A later 'off' rule must remove a day an earlier range already set."""
    assert _spans('Mo-Su 09:00-17:00; We off') == {
        'mon': '09:00-17:00', 'tue': '09:00-17:00', 'thu': '09:00-17:00',
        'fri': '09:00-17:00', 'sat': '09:00-17:00', 'sun': '09:00-17:00'}


def test_osm_hours_wrapping_range():
    assert set(parse_osm_hours('Sa-Su 10:00-16:00')) == {'sat', 'sun'}
    assert set(parse_osm_hours('Fr-Mo 10:00-16:00')) == {'fri', 'sat', 'sun', 'mon'}


def test_osm_hours_holiday_rules_ignored():
    """PH/SH describe exceptions, not the weekly pattern."""
    assert _spans('Mo-Fr 09:00-17:00; PH off') == {
        'mon': '09:00-17:00', 'tue': '09:00-17:00', 'wed': '09:00-17:00',
        'thu': '09:00-17:00', 'fri': '09:00-17:00'}
    assert parse_osm_hours('PH 10:00-14:00') == {}


def test_osm_hours_rejects_inverted_span():
    """Real AU data carries '09:00-04:45' (a PM time typo). Showing a shop as open
    at 2am is worse than showing no hours."""
    assert parse_osm_hours('Mo-Fr 09:00-04:45') == {}


def test_osm_hours_pads_single_digit_hour():
    assert _spans('Mo 9:00-17:00') == {'mon': '09:00-17:00'}


def test_osm_hours_unsupported_syntax_yields_empty():
    for spec in ('24/7', 'sunrise-sunset', 'Jan-Mar 10:00-16:00', '', None):
        assert parse_osm_hours(spec) == {}, spec


# ----- vinnies_hours -----

def test_vinnies_hours_current_list_schema():
    """Sept 2026 shape: flat list, 'name'/'scheduled', HH:MM:SS."""
    oh = [
        {'name': 'Monday', 'scheduled': True, 'open': '09:00:00', 'close': '17:00:00'},
        {'name': 'Sunday', 'scheduled': False, 'open': '10:00:00', 'close': '14:00:00'},
    ]
    assert vinnies_hours(oh) == {'mon': {'o': '09:00', 'c': '17:00'}}


def test_vinnies_hours_legacy_dict_schema():
    oh = {'openingTimes': [
        {'weekday': 'Tuesday', 'isScheduled': True, 'open': '08:30', 'close': '16:30'},
    ]}
    assert vinnies_hours(oh) == {'tue': {'o': '08:30', 'c': '16:30'}}


def test_vinnies_hours_empty():
    assert vinnies_hours(None) == {}
    assert vinnies_hours([]) == {}


def test_vinnies_hours_rejects_inverted_span():
    """Real upstream data carries '09:00-05:00' (a PM close tagged as AM). Two shops
    shipped hours claiming they were open overnight."""
    oh = [
        {'name': 'Monday', 'scheduled': True, 'open': '09:00:00', 'close': '05:00:00'},
        {'name': 'Tuesday', 'scheduled': True, 'open': '09:00:00', 'close': '17:00:00'},
    ]
    assert vinnies_hours(oh) == {'tue': {'o': '09:00', 'c': '17:00'}}


def test_vinnies_hours_skips_unscheduled_and_missing_times():
    oh = [
        {'name': 'Monday', 'scheduled': True, 'open': None, 'close': '17:00:00'},
        {'name': 'Tuesday', 'scheduled': False, 'open': '09:00:00', 'close': '17:00:00'},
    ]
    assert vinnies_hours(oh) == {}


# ----- parse_vinnies_page -----

def _page(page_data):
    return ('<html><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps({'props': {'pageProps': {'pageData': page_data}}})
            + '</script></html>')


def test_parse_vinnies_page_current_schema():
    html = _page({
        'shopName': 'Vinnies Bega',
        'phoneNumber': '(02) 6234 7485',
        'addressLineOne': '130 Gipps St', 'addressSuburb': 'Bega',
        'addressState': 'NSW', 'addressPostcode': '2550',
        'location': {'address': {'coordinates': {'lat': -36.677907, 'lng': 149.842829}}},
        'openingHours': [{'name': 'Monday', 'scheduled': True, 'open': '09:00:00', 'close': '16:30:00'}],
    })
    r = parse_vinnies_page(html)
    assert r['name'] == 'Vinnies Bega'
    assert (r['lat'], r['lon']) == (-36.677907, 149.842829)
    assert r['suburb'] == 'Bega'
    assert r['hours'] == {'mon': {'o': '09:00', 'c': '16:30'}}


def test_parse_vinnies_page_no_coords_returns_none():
    assert parse_vinnies_page(_page({'shopName': 'X', 'location': {}})) is None


def test_parse_vinnies_page_no_next_data_returns_none():
    assert parse_vinnies_page('<html>nothing here</html>') is None


# ----- salvos_street_variants -----
# Streets taken from the 155 upstream Salvos records Nominatim could not resolve.

def test_salvos_variants_try_original_first():
    """A plain street must be queried unchanged — stripping is a fallback, never the
    first guess, or a real street number could be mistaken for a unit number."""
    assert salvos_street_variants('192 Lava Street')[0] == '192 Lava Street'


def test_salvos_variants_strip_shop_comma():
    assert '60 Mill Street' in salvos_street_variants('Shop 1, 60 Mill Street')


def test_salvos_variants_strip_shop_slash():
    assert '130 High Street' in salvos_street_variants('Shop 16/130 High Street')


def test_salvos_variants_strip_bare_unit_slash():
    assert '308 Kororoit Creek' in salvos_street_variants('1&2/308 Kororoit Creek')


def test_salvos_variants_strip_tenancy_code():
    assert '322 Hale Road' in salvos_street_variants('T3-5 / 322 Hale Road')


def test_salvos_variants_strip_unit_comma():
    assert '10 - 16  Medcalf Street' in salvos_street_variants('Unit 2, 10 - 16  Medcalf Street')


def test_salvos_variants_are_unique_and_nonempty():
    for street in ('192 Lava Street', 'Shop 1, 60 Mill Street', '', None):
        v = salvos_street_variants(street)
        assert len(v) == len(set(v))
        assert all(s.strip() for s in v)


def test_salvos_variants_empty_street_yields_nothing():
    """An empty street must not produce a bare ', Australia' query."""
    assert salvos_street_variants('') == []
    assert salvos_street_variants(None) == []


# ----- redcross_hours -----

def test_redcross_hours_bare_12h_close_becomes_pm():
    h = redcross_hours([{'day': 'Tuesday', 'closed': False,
                         'opens': '9:00', 'closes': '5:00'}])
    assert h == {'tue': {'o': '09:00', 'c': '17:00'}}


def test_redcross_hours_already_24h_close_untouched():
    h = redcross_hours([{'day': 'Monday', 'closed': False,
                         'opens': '09:30', 'closes': '17:30'}])
    assert h == {'mon': {'o': '09:30', 'c': '17:30'}}


def test_redcross_hours_midday_close_is_not_shifted():
    h = redcross_hours([{'day': 'Saturday', 'closed': False,
                         'opens': '9:00', 'closes': '12:00'}])
    assert h == {'sat': {'o': '09:00', 'c': '12:00'}}


def test_redcross_hours_closed_day_dropped():
    assert redcross_hours([{'day': 'Sunday', 'closed': True,
                            'opens': '', 'closes': ''}]) == {}


def test_redcross_hours_missing_times_dropped():
    assert redcross_hours([{'day': 'Monday', 'closed': False,
                            'opens': '', 'closes': ''}]) == {}


def test_redcross_hours_unknown_day_dropped():
    assert redcross_hours([{'day': 'Someday', 'closed': False,
                            'opens': '9:00', 'closes': '5:00'}]) == {}


def test_redcross_hours_junk_times_dropped():
    assert redcross_hours([{'day': 'Monday', 'closed': False,
                            'opens': 'ab:cd', 'closes': '5:00'}]) == {}


def test_redcross_hours_not_a_list():
    assert redcross_hours(None) == {}


# ----- parse_redcross_page -----

RC_PAGE = ('<html><body><script>window.__DATA={"components":[{'
           '"name":"LocationDetails","props":{"id":"abc",'
           '"name":"Red Cross Shop Albany","type":"retailstore",'
           '"coordinates":{"latitude":-35.0182426,"longitude":117.88443},'
           '"streetAddress":"Shop 2, 78 Lockyer Avenue","locality":"Albany",'
           '"postalCode":"6330","state":"WA","phone":"08 6834 8830",'
           '"operatingHours":[{"day":"Monday","closed":true,"opens":"","closes":""},'
           '{"day":"Tuesday","closed":false,"opens":"9:00","closes":"5:00"}],'
           '"paymentTypes":["Cash"]}}]}</script></body></html>')


def test_parse_redcross_page_extracts_shop_not_component_name():
    d = parse_redcross_page(RC_PAGE)
    # The component's own "name":"LocationDetails" precedes the shop's name.
    assert d['name'] == 'Red Cross Shop Albany'


def test_parse_redcross_page_address_fields():
    d = parse_redcross_page(RC_PAGE)
    assert (d['address'], d['suburb'], d['state'], d['postcode']) == (
        'Shop 2, 78 Lockyer Avenue', 'Albany', 'WA', '6330')
    assert d['phone'] == '08 6834 8830'
    assert (d['lat'], d['lon']) == (-35.018243, 117.88443)
    assert d['chain'] == 'redcross'


def test_parse_redcross_page_hours():
    assert parse_redcross_page(RC_PAGE)['hours'] == {
        'tue': {'o': '09:00', 'c': '17:00'}}


def test_parse_redcross_page_no_blob_returns_none():
    assert parse_redcross_page('<html><body>nothing here</body></html>') is None


def test_parse_redcross_page_no_coords_returns_none():
    assert parse_redcross_page(
        '<html><script>{"name":"LocationDetails","props":{'
        '"name":"X","streetAddress":"1 Test St"}}</script></html>') is None


# ----- dist_km -----

def test_dist_km_same_point():
    p = {'lat': -37.81, 'lon': 144.96}
    assert dist_km(p, p) == 0


def test_dist_km_melbourne_to_sydney():
    mel = {'lat': -37.8136, 'lon': 144.9631}
    syd = {'lat': -33.8688, 'lon': 151.2093}
    # Great-circle Melbourne-Sydney is ~714 km
    assert 700 < dist_km(mel, syd) < 730


def test_dist_km_symmetric():
    a = {'lat': -37.81, 'lon': 144.96}
    b = {'lat': -34.0, 'lon': 151.0}
    assert abs(dist_km(a, b) - dist_km(b, a)) < 0.001


# ----- dedupe -----

def _shop(lat, lon, chain, source, name='X', operator=''):
    return {
        'lat': lat, 'lon': lon, 'chain': chain, 'source': source,
        'name': name, 'operator': operator,
    }


def test_dedupe_same_chain_same_spot():
    """Two Vinnies at the same coords collapse; higher-priority source wins."""
    shops = [
        _shop(-37.81, 144.96, 'vinnies', 'osm'),
        _shop(-37.81, 144.96, 'vinnies', 'vinnies'),
    ]
    kept = dedupe(shops)
    assert len(kept) == 1
    assert kept[0]['source'] == 'vinnies'


def test_dedupe_different_chains_same_spot():
    """Vinnies and Salvos at same coords are NOT duplicates."""
    shops = [
        _shop(-37.81, 144.96, 'vinnies', 'vinnies'),
        _shop(-37.81, 144.96, 'salvos', 'salvos'),
    ]
    kept = dedupe(shops)
    assert len(kept) == 2


def test_dedupe_far_apart_kept():
    """Shops >150m apart with same chain are both kept."""
    shops = [
        _shop(-37.81, 144.96, 'vinnies', 'vinnies'),
        _shop(-37.82, 144.98, 'vinnies', 'osm'),  # ~2km away
    ]
    kept = dedupe(shops)
    assert len(kept) == 2


def test_dedupe_independent_never_collapses():
    """Two 'independent' shops at same coords stay separate — no chain match."""
    shops = [
        _shop(-37.81, 144.96, 'independent', 'osm', name='A'),
        _shop(-37.81, 144.96, 'independent', 'osm', name='B'),
    ]
    kept = dedupe(shops)
    assert len(kept) == 2


def test_dedupe_osm_named_vinnies_drops_against_real():
    """OSM shop named 'St Vincent de Paul' near a scraped Vinnies is dropped."""
    shops = [
        _shop(-37.81, 144.96, 'vinnies', 'vinnies', name='Vinnies Melbourne'),
        _shop(-37.8101, 144.9601, 'independent', 'osm', name='St Vincent de Paul Shop', operator='SVdP'),
    ]
    kept = dedupe(shops)
    assert len(kept) == 1
    assert kept[0]['source'] == 'vinnies'


def test_dedupe_salvos_wider_radius():
    """Salvos geocoded coords can be ~250m off; wider match radius applies."""
    shops = [
        _shop(-37.81, 144.96, 'salvos', 'salvos', name='Salvos Melbourne'),
        _shop(-37.8115, 144.9625, 'independent', 'osm', name='Salvation Army Store'),
    ]
    kept = dedupe(shops)
    assert len(kept) == 1
    assert kept[0]['source'] == 'salvos'


# ----- compact_records -----

def _full(**overrides):
    base = {
        'name': 'X', 'lat': -37.8, 'lon': 144.9, 'source': 'osm',
        'operator': '', 'chain': 'independent',
        'address': '', 'suburb': '', 'state': '', 'postcode': '',
        'phone': '', 'hours': {},
    }
    base.update(overrides)
    return base


def test_compact_strips_empty_fields():
    out = compact_records([_full()])
    assert out == [{'n': 'X', 'y': -37.8, 'x': 144.9, 'src': 'osm'}]


def test_compact_omits_independent_chain():
    """chain=independent isn't worth storing — clients treat missing as independent."""
    out = compact_records([_full(chain='independent', operator='Some Op')])
    assert 'c' not in out[0]
    assert out[0]['o'] == 'Some Op'


def test_compact_keeps_named_chain():
    out = compact_records([_full(chain='vinnies', operator='Vinnies')])
    assert out[0]['c'] == 'vinnies'


def test_compact_keeps_hours():
    hrs = {'mon': {'o': '09:00', 'c': '17:00'}}
    out = compact_records([_full(hours=hrs)])
    assert out[0]['h'] == hrs


# ----- assert_quality -----
# The live run on 2026-10-12. Every case below perturbs one field of this baseline,
# so retuning a threshold doesn't need nine literals edited in lockstep.
OK = {'vinnies': 452, 'redcross': 178, 'salvos': 439, 'osm': 731}
OK_TOTAL, OK_HOURS, OK_ADDR = 1800, 1214, 1338


def test_quality_passes_at_baseline():
    assert_quality(OK, OK_TOTAL, with_hours=OK_HOURS, with_address=OK_ADDR)


def test_quality_fails_when_vinnies_missing(capsys):
    import pytest
    with pytest.raises(SystemExit):
        assert_quality({**OK, 'vinnies': 10}, OK_TOTAL,
                       with_hours=OK_HOURS, with_address=OK_ADDR)
    assert 'vinnies' in capsys.readouterr().err


def test_quality_fails_when_salvos_geocode_regresses(capsys):
    """Street-only geocoding placed 317 of 472 Salvos records and silently dropped the
    rest. That must not read as a healthy run."""
    import pytest
    with pytest.raises(SystemExit):
        assert_quality({**OK, 'salvos': 317}, 1710,
                       with_hours=OK_HOURS, with_address=1219)
    err = capsys.readouterr().err
    assert 'salvos' in err


def test_quality_fails_when_total_low(capsys):
    import pytest
    with pytest.raises(SystemExit):
        assert_quality({'vinnies': 400, 'redcross': 150, 'salvos': 420, 'osm': 700}, 500)
    assert 'total_kept' in capsys.readouterr().err


def test_quality_fails_when_hours_vanish(capsys):
    """Counts can all pass while the hours sub-schema is broken — the 'Open now'
    filter would silently match nothing. That must fail the run."""
    import pytest
    with pytest.raises(SystemExit):
        assert_quality(OK, OK_TOTAL, with_hours=0, with_address=OK_ADDR)
    assert 'with_hours' in capsys.readouterr().err


def test_quality_fails_when_osm_hours_stop_arriving(capsys):
    """Losing the OSM tags or the dedupe merge still leaves the Vinnies-only hours
    well above zero, so the guard has to sit above that floor to notice."""
    import pytest
    with pytest.raises(SystemExit):
        assert_quality(OK, OK_TOTAL, with_hours=375, with_address=OK_ADDR)
    assert 'with_hours' in capsys.readouterr().err


def test_quality_fails_when_addresses_vanish(capsys):
    """The measured state before the Oct 2026 parser fixes: every shop count passes
    while 695 of 1733 shops render a name and nothing else."""
    import pytest
    with pytest.raises(SystemExit):
        assert_quality(OK, OK_TOTAL, with_hours=OK_HOURS, with_address=767)
    assert 'with_address' in capsys.readouterr().err


def test_quality_fails_when_one_source_loses_addresses(capsys):
    """Losing just the Red Cross blob again leaves ~1187 — the guard has to sit above
    that, not merely above zero."""
    import pytest
    with pytest.raises(SystemExit):
        assert_quality(OK, OK_TOTAL, with_hours=685, with_address=1187)
    err = capsys.readouterr().err
    assert 'with_address' in err and 'with_hours' in err


def test_quality_skips_optional_checks_when_not_supplied():
    """Callers that don't measure a field must not trip its guard."""
    assert_quality(OK, OK_TOTAL)


def test_quality_baseline_clears_every_threshold_with_margin():
    """A floor set at or above the live value is a guard that fails on the next good
    run. Catch that at edit time rather than next Sunday."""
    actual = {**OK, 'total_kept': OK_TOTAL,
              'with_hours': OK_HOURS, 'with_address': OK_ADDR}
    for k, threshold in MIN_COUNTS.items():
        assert threshold < actual[k], f'{k}: floor {threshold} >= live {actual[k]}'
