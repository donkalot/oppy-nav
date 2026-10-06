"""Unit tests for pure helpers in scrape_all.py.

Run: pytest scripts/test_scrape.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import json

from scrape_all import (
    dist_km, dedupe, compact_records, assert_quality, MIN_COUNTS,
    vinnies_hours, parse_vinnies_page, parse_osm_hours,
)


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

def test_quality_passes_at_baseline():
    by_source = {'vinnies': 453, 'redcross': 177, 'salvos': 310, 'osm': 757}
    assert_quality(by_source, 1697)  # should not raise / exit


def test_quality_fails_when_vinnies_missing(capsys):
    import pytest
    by_source = {'vinnies': 10, 'redcross': 177, 'salvos': 310, 'osm': 757}
    with pytest.raises(SystemExit):
        assert_quality(by_source, 1697)
    err = capsys.readouterr().err
    assert 'vinnies' in err


def test_quality_fails_when_total_low(capsys):
    import pytest
    by_source = {'vinnies': 400, 'redcross': 150, 'salvos': 280, 'osm': 700}
    with pytest.raises(SystemExit):
        assert_quality(by_source, 500)
    assert 'total_kept' in capsys.readouterr().err


def test_quality_fails_when_hours_vanish(capsys):
    """Counts can all pass while the hours sub-schema is broken — the 'Open now'
    filter would silently match nothing. That must fail the run."""
    import pytest
    by_source = {'vinnies': 452, 'redcross': 178, 'salvos': 315, 'osm': 743}
    with pytest.raises(SystemExit):
        assert_quality(by_source, 1688, with_hours=0)
    assert 'with_hours' in capsys.readouterr().err


def test_quality_passes_with_healthy_hours():
    by_source = {'vinnies': 452, 'redcross': 178, 'salvos': 315, 'osm': 743}
    assert_quality(by_source, 1688, with_hours=449)
