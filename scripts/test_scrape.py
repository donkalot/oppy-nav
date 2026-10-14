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
    uplister_hours, SALVOS_TEST_RECORD, reverse_geocode_missing, AU_STATES,
    reverse_state, merge_street_twins, same_street, house_number,
    parse_osm_elements, CHAIN_PATTERNS,
)
import datetime
import re


# ----- reverse_geocode_missing -----
# 461 OSM shops carry coords but no addr:* tags at all, so no parser change reaches
# them. These tests cover the cache-application half; the network half is the fetch.

def _cache_file(tmp_path, payload):
    p = tmp_path / 'rev.json'
    p.write_text(json.dumps(payload), encoding='utf-8')
    return p


def _bare(**kw):
    base = {'name': 'Op shop', 'lat': -37.81449, 'lon': 144.96073,
            'address': '', 'suburb': '', 'state': '', 'postcode': ''}
    base.update(kw)
    return base


def test_reverse_geocode_fills_and_flags(tmp_path):
    """A filled address must be marked so the popup can say where it came from — the
    street is whatever sits at that coordinate, which can be the building next door."""
    cache = _cache_file(tmp_path, {'-37.81449,144.96073': {
        'address': '117-119 Maitland Road', 'suburb': 'Mayfield',
        'state': 'NSW', 'postcode': '2304'}})
    s = _bare()
    reverse_geocode_missing([s], cache)
    assert s['address'] == '117-119 Maitland Road'
    assert s['suburb'] == 'Mayfield' and s['state'] == 'NSW'
    assert s['address_approx'] is True


def test_reverse_geocode_never_overwrites_an_upstream_address(tmp_path):
    """Upstream data always wins: a published address is authoritative over one derived
    from coordinates, and must not be flagged approximate."""
    cache = _cache_file(tmp_path, {'-37.81449,144.96073': {
        'address': '1 Wrong Street', 'suburb': 'Elsewhere', 'state': 'NSW',
        'postcode': '9999'}})
    s = _bare(address='42 Real Road', suburb='Truthville')
    reverse_geocode_missing([s], cache)
    assert s['address'] == '42 Real Road' and s['suburb'] == 'Truthville'
    assert 'address_approx' not in s


def test_reverse_geocode_ignores_an_empty_cache_hit(tmp_path):
    """Nominatim can resolve a coordinate to nothing useful. Flagging that as an
    approximate address would put an empty "(address from map)" note in the popup."""
    cache = _cache_file(tmp_path, {'-37.81449,144.96073': {
        'address': '', 'suburb': '', 'state': '', 'postcode': ''}})
    s = _bare()
    reverse_geocode_missing([s], cache)
    assert 'address_approx' not in s


def test_reverse_geocode_accepts_a_suburb_only_hit(tmp_path):
    """Street unknown but suburb known still beats a name-only popup."""
    cache = _cache_file(tmp_path, {'-37.81449,144.96073': {
        'address': '', 'suburb': 'Mayfield', 'state': 'NSW', 'postcode': '2304'}})
    s = _bare()
    reverse_geocode_missing([s], cache)
    assert s['suburb'] == 'Mayfield' and s['address_approx'] is True


def test_reverse_geocode_cache_key_is_coordinate_rounded(tmp_path):
    """Keyed on 5dp so a shop whose coords wobble in the 6th place still hits cache
    rather than spending another 1.1s lookup every week."""
    cache = _cache_file(tmp_path, {'-37.81449,144.96073': {
        'address': '1 Real Road', 'suburb': 'Mayfield', 'state': 'NSW', 'postcode': ''}})
    s = _bare(lat=-37.814494, lon=144.960731)
    reverse_geocode_missing([s], cache)
    assert s['address'] == '1 Real Road'


def test_au_states_covers_every_state_nominatim_returns():
    """Nominatim returns full names; every other source emits the abbreviation and the
    app's state filter matches on it, so an unmapped state silently empties the field."""
    assert set(AU_STATES.values()) == {'NSW', 'VIC', 'QLD', 'SA', 'WA', 'TAS',
                                       'ACT', 'NT'}
    assert AU_STATES['new south wales'] == 'NSW'


def test_reverse_state_prefers_the_iso_code():
    assert reverse_state({'ISO3166-2-lvl4': 'AU-NSW', 'state': 'Victoria'}) == 'NSW'


def test_reverse_state_reads_the_act_from_territory():
    """Measured on a real Canberra lookup: the ACT comes back under `territory`, with
    no `state` key at all, so reading `state` alone blanks every Canberra shop."""
    a = {'territory': 'Australian Capital Territory', 'suburb': 'Dickson'}
    assert reverse_state(a) == 'ACT'


def test_reverse_state_handles_an_external_territory():
    """Norfolk Island reports territory "Norfolk Island" and no ISO lvl4. It's a real
    Australian shop, so an empty state is correct — but it must not raise."""
    assert reverse_state({'territory': 'Norfolk Island'}) == ''
    assert reverse_state({}) == ''


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


# ----- merge_street_twins -----
# 18 twins at 166–932m survived the radius pass in the 2026-10-15 data: shopping-centre
# shops that OSM tags at the mall centroid while the operator gives the tenancy.

def _tw(source, chain, address, dlat=0.0, approx=False, **kw):
    s = {'lat': -37.81 + dlat, 'lon': 144.96, 'source': source, 'chain': chain,
         'name': kw.pop('name', source), 'operator': '', 'address': address, **kw}
    if approx:
        s['address_approx'] = True
    return s


def test_street_twin_mall_centroid_merges():
    """Wantirna South, measured: OSM 499m from the tenancy, same Burwood Highway number."""
    shops = [_tw('salvos', 'salvos', 'Knox Westfield Ozone, Shop 8133/ 425 Burwood Highway'),
             _tw('osm', 'salvos', '425 Burwood Highway', dlat=0.0045, approx=True)]
    kept = merge_street_twins(shops)
    assert [s['source'] for s in kept] == ['salvos']


def test_street_twin_conflicting_numbers_kept():
    """Essendon North: 312 and 481A Keilor Road are two shops, not one shop twice."""
    shops = [_tw('vinnies', 'vinnies', '312 Keilor Road'),
             _tw('osm', 'vinnies', '481A Keilor Road', dlat=0.004)]
    assert len(merge_street_twins(shops)) == 2


def test_street_twin_reverse_geocoded_number_never_vetoes():
    """A backfilled number is whatever building sits at the OSM point — 27 Griffith Street
    for a shop at 80 — so it can't be evidence of a second shop."""
    shops = [_tw('redcross', 'redcross', '80 Griffith Street'),
             _tw('osm', 'redcross', '27 Griffith Street', dlat=0.0015, approx=True)]
    assert len(merge_street_twins(shops)) == 1


def test_street_twin_number_inside_a_range_matches():
    shops = [_tw('salvos', 'salvos', '499 High Street'),
             _tw('osm', 'salvos', '499-505 High Street', dlat=0.0035)]
    assert len(merge_street_twins(shops)) == 1


def test_street_twin_directional_suffix_is_a_different_road():
    """St Albans: Main Road East and Main Road West are separate roads 521m apart."""
    shops = [_tw('vinnies', 'vinnies', '276-278 Main Road East'),
             _tw('osm', 'vinnies', 'Main Road West', dlat=0.0045, approx=True)]
    assert len(merge_street_twins(shops)) == 2


def test_street_twin_needs_same_chain():
    shops = [_tw('vinnies', 'vinnies', '10 Marshall Street'),
             _tw('osm', 'salvos', '10 Marshall Street', dlat=0.003)]
    assert len(merge_street_twins(shops)) == 2


def test_street_twin_never_merges_two_official_records():
    """Two Red Cross shops in Fremantle are both real listings; only OSM is redundant."""
    shops = [_tw('redcross', 'redcross', '37 Market Street'),
             _tw('redcross', 'redcross', '37 Market Street', dlat=0.003)]
    assert len(merge_street_twins(shops)) == 2


def test_street_twin_respects_max_distance():
    shops = [_tw('salvos', 'salvos', '27 Windsor Street'),
             _tw('osm', 'salvos', '27 Windsor Street', dlat=0.02)]  # ~2.2km
    assert len(merge_street_twins(shops)) == 2


def test_street_twin_inherits_hours_from_the_dropped_record():
    shops = [_tw('vinnies', 'vinnies', '260 Sydney Road', hours={}),
             _tw('osm', 'vinnies', '260 Sydney Road', dlat=0.0038,
                 hours={'mon': {'o': '09:00', 'c': '17:00'}})]
    kept = merge_street_twins(shops)
    assert kept[0]['hours'] == {'mon': {'o': '09:00', 'c': '17:00'}}


@pytest.mark.parametrize('a,b', [
    ('710 Old Princes Hwy', '710 Old Princes Highway'),
    ('54 Red Gate Road', 'Redgate Road'),
    ('Broadway Street', 'Broadway'),
    ('Shop 1-2, 89 John St', 'John Street'),
    ('Ritz Arcade, Dean Street', 'Ritz Arcade'),
    ("122 Walker  Street", 'Walker Street'),
])
def test_same_street_agrees(a, b):
    assert same_street(a, b)


@pytest.mark.parametrize('a,b', [
    ('276-278 Main Road East', 'Main Road West'),
    ('42 Victoria Street', '81 Victoria Crescent'),
    ('Unit 1 12 Belford Pl', '96 Mitchell Rd'),
])
def test_same_street_disagrees(a, b):
    assert not same_street(a, b)


@pytest.mark.parametrize('addr,want', [
    ('4/218 High Street', (218, 218)),
    ('499-505 High Street', (499, 505)),
    ('481A Keilor Road', (481, 481)),
    ('Knox Westfield Ozone, Shop 8133/ 425 Burwood Highway', (425, 425)),
    ('Broadway Street', None),
])
def test_house_number(addr, want):
    assert house_number(addr) == want


# ----- chain patterns -----

def _chain_of(name, operator=''):
    data = {'elements': [{'type': 'node', 'lat': -37.8, 'lon': 144.9,
                          'tags': {'name': name, 'operator': operator}}]}
    return parse_osm_elements(data)[0]['chain']


@pytest.mark.parametrize('name,operator,want', [
    ('Save the Children Op Shop', '', 'savethechildren'),
    ('Save the Chikldren Op-Shop', '', 'savethechildren'),
    ("Good Sammy's", 'Rivers', 'goodsammy'),
    ('Annie\'s Place', "St. Luke's Anglican Parish", 'anglican'),
    ('Carrum Op Shop', 'Carrum Uniting Church', 'uniting'),
    ('Lions Club of Northcote Op Shop', '', 'lions'),
    ('Penguin Lions Op-Shop', '', 'lions'),
    # Originals win over the new patterns: first match in list order.
    ('Lifeline Shop', 'Uniting', 'lifeline'),
    # A shop name, not a service club.
    ('The Lions Den Op Shop', '', 'independent'),
])
def test_chain_patterns(name, operator, want):
    assert _chain_of(name, operator) == want


def test_every_chain_id_has_a_client_label():
    """An unlabelled chain id renders as the raw id ("goodsammy") in the filter list."""
    html = (pathlib.Path(__file__).parent.parent / 'index.html').read_text(encoding='utf-8')
    block = re.search(r'const CHAIN_LABELS = \{(.*?)\};', html, re.S).group(1)
    labelled = set(re.findall(r'(\w+)\s*:', block))
    missing = {cid for _, cid in CHAIN_PATTERNS} - labelled
    assert not missing


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
