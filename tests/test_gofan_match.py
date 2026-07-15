"""Offline regression tests for the matcher.

Every expectation here is pinned to a real, observed GoFan response -- these are
the cases that actually bit during development.

    .venv/bin/python -m pytest tests/ -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gofan_match as gm


def test_parse_address():
    assert gm.parse_address("8141 Lone Star Rd, Jacksonville, FL 32211") == (
        "Jacksonville",
        "FL",
        "32211",
    )
    assert gm.parse_address("2600 Mayport Rd, Atlantic Beach, FL 32233") == (
        "Atlantic Beach",
        "FL",
        "32233",
    )
    assert gm.parse_address("") == (None, None, None)
    assert gm.parse_address("not an address") == (None, None, None)


def test_logo_url_is_percent_encoded():
    """12 of 24 Duval logo URLs contain spaces; raw they 404."""
    raw = "https://production-gofan-assets.s3.amazonaws.com/uploads/school/logo/FL25635/black Mhawks logo.jpg"
    assert gm.encode_logo_url(raw).endswith("/black%20Mhawks%20logo.jpg")
    # Already-encoded and empty input must survive untouched.
    assert gm.encode_logo_url("") == ""


def test_placeholder_detection():
    assert gm.is_placeholder_logo(".../uploads/school/logo/gofan-logo-black.png")
    assert not gm.is_placeholder_logo(".../uploads/school/logo/FL25635/mhawks.jpg")


def test_state_mismatch_is_rejected():
    """The core guard: Arlington Middle exists in both TN and FL."""
    tn = {"huddleId": "TN73539", "city": "Arlington", "state": "TN", "zipCode": "38002"}
    assert not gm.verify(tn, "Jacksonville", "FL", "32211")

    fl = {"huddleId": "FL25617", "city": "JACKSONVILLE", "state": "FL", "zipCode": "32211"}
    assert gm.verify(fl, "Jacksonville", "FL", "32211")


def test_zip_agreement_alone_is_enough():
    c = {"city": "JAX", "state": "FL", "zipCode": "32257"}
    assert gm.verify(c, "Jacksonville", "FL", "32257")


def test_city_prefix_is_last_resort_only():
    """Jacksonville vs Jacksonville Beach share a 4-char prefix.

    Tolerated loosely, but never under strict (bare single-token) queries.
    """
    beach = {"city": "JACKSONVILLE BEACH", "state": "FL", "zipCode": "32250"}
    assert gm.verify(beach, "Jacksonville", "FL", "32211", strict=False)
    assert not gm.verify(beach, "Jacksonville", "FL", "32211", strict=True)


def test_ladder_reaches_gofans_own_names():
    """GoFan renames schools; the ladder must produce the query that finds them."""
    expectations = {
        "Julia Landon College Preparatory Middle School": "Landon",
        "Duncan U. Fletcher Middle School": "Duncan Fletcher Middle School",
        "Mayport Coastal Sciences Middle School": "Mayport Middle School",
        "Matthew W. Gilbert Middle School": "Matthew Gilbert Middle School",
        "Darnell-Cookman School of the Medical Arts": "Darnell Cookman",
    }
    for name, needed in expectations.items():
        ladder = [q.lower() for q in gm.query_ladder(name, city="Jacksonville", state="FL")]
        assert needed.lower() in ladder, f"{name!r} ladder missing {needed!r}: {ladder}"


def test_ladder_never_searches_a_bare_city_name():
    """Regression: 'Jacksonville STEM Academy' matched 'Duval County Middle School
    Conference' because the ladder fell back to the bare token 'Jacksonville',
    and every Jacksonville org then passed the city gate."""
    ladder = gm.query_ladder(
        "Jacksonville STEM Academy (Young Men's & Women's Leadership Academy)",
        city="Jacksonville",
        state="FL",
    )
    assert "jacksonville" not in [q.lower() for q in ladder]
    assert "STEM" in ladder


def test_ladder_drops_parenthetical_alias():
    ladder = gm.query_ladder("Jacksonville STEM Academy (Young Men's)", city="Jacksonville", state="FL")
    assert "Jacksonville STEM Academy" in ladder


def test_strict_flag_tracks_single_token_queries():
    assert gm.is_strict_query("Landon")
    assert not gm.is_strict_query("Landon Middle School")


def test_pick_returns_first_verified_candidate():
    cands = [
        {"huddleId": "TX1", "city": "Bellaire", "state": "TX", "zipCode": "77401"},
        {"huddleId": "FL25635", "city": "JACKSONVILLE", "state": "FL", "zipCode": "32257"},
    ]
    hit = gm.pick(cands, "Jacksonville", "FL", "32257")
    assert hit["huddleId"] == "FL25635"
    assert gm.pick(cands, "Miami", "FL", "33101") is None


def test_school_url():
    assert gm.school_url("FL25635") == "https://gofan.co/app/school/FL25635"
