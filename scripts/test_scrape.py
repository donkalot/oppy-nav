"""Unit tests for pure helpers in scrape_all.py.

Run: pytest scripts/test_scrape.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from scrape_all import dist_km, dedupe, compact_records, assert_quality, MIN_COUNTS


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
