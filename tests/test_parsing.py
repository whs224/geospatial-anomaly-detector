"""Unit tests for OpenSky state-vector parsing."""

from datetime import datetime, timezone

from main import parse_flight_state

# A representative airborne state vector (OpenSky /states/all row format).
VALID_STATE = [
    '4b1814',        # icao24
    'SWR123  ',      # callsign (padded, as OpenSky returns it)
    'Switzerland',   # origin_country
    1751900000,      # time_position
    1751900000,      # last_contact
    8.55,            # longitude
    47.45,           # latitude
    11582.4,         # baro_altitude
    False,           # on_ground
    231.5,           # velocity
    275.9,           # heading
    0.0,             # vertical_rate
    None,            # sensors
    11887.2,         # geo_altitude
    '1000',          # squawk
    False,           # spi
    0,               # position_source
]


def make_state(**overrides):
    indices = {
        'icao24': 0, 'callsign': 1, 'last_contact': 4, 'longitude': 5,
        'latitude': 6, 'on_ground': 8, 'velocity': 9, 'heading': 10,
    }
    state = list(VALID_STATE)
    for name, value in overrides.items():
        state[indices[name]] = value
    return state


def test_parses_valid_vector():
    parsed = parse_flight_state(VALID_STATE)
    assert parsed == {
        'icao24': '4b1814',
        'callsign': 'SWR123',
        'velocity': 231.5,
        'heading': 275.9,
        'last_contact': datetime.fromtimestamp(1751900000, tz=timezone.utc),
        'longitude': 8.55,
        'latitude': 47.45,
    }


def test_timestamp_is_timezone_aware_utc():
    parsed = parse_flight_state(VALID_STATE)
    assert parsed['last_contact'].tzinfo == timezone.utc


def test_missing_coordinates_rejected():
    assert parse_flight_state(make_state(latitude=None)) is None
    assert parse_flight_state(make_state(longitude=None)) is None


def test_on_ground_traffic_rejected():
    assert parse_flight_state(make_state(on_ground=True)) is None


def test_missing_last_contact_rejected():
    assert parse_flight_state(make_state(last_contact=None)) is None


def test_missing_icao24_rejected():
    assert parse_flight_state(make_state(icao24=None)) is None
    # Empty string is part of the primary key and must be rejected too.
    assert parse_flight_state(make_state(icao24='')) is None


def test_zero_velocity_preserved():
    parsed = parse_flight_state(make_state(velocity=0.0))
    assert parsed['velocity'] == 0.0


def test_null_velocity_and_heading_kept_as_none():
    parsed = parse_flight_state(make_state(velocity=None, heading=None))
    assert parsed['velocity'] is None
    assert parsed['heading'] is None


def test_blank_callsign_becomes_none():
    parsed = parse_flight_state(make_state(callsign='        '))
    assert parsed['callsign'] is None
    parsed = parse_flight_state(make_state(callsign=None))
    assert parsed['callsign'] is None


def test_malformed_vectors_rejected():
    assert parse_flight_state([]) is None
    assert parse_flight_state(['4b1814', 'SWR123']) is None
    assert parse_flight_state(None) is None
