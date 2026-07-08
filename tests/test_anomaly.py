"""Unit tests for anomaly evidence construction and summary formatting."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from anomaly import build_anomaly_records, format_summary

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)


def make_row(prev_v=168.3, new_v=232.0, gap=10.0):
    seconds = float(gap) if gap is not None else 0.0
    return ('4b1814', 'SWR123', prev_v, new_v,
            T0, T0 + timedelta(seconds=seconds), gap)


def test_builds_record_with_implied_acceleration():
    records = build_anomaly_records([make_row()], threshold=2.0)
    assert len(records) == 1
    record = records[0]
    assert record['icao24'] == '4b1814'
    assert record['prev_velocity'] == 168.3
    assert record['new_velocity'] == 232.0
    assert record['delta_v'] == 63.7
    assert record['time_gap_seconds'] == 10.0
    assert record['implied_accel'] == 6.37
    assert record['threshold'] == 2.0
    # Keys the detector persists / dedupes on — a refactor dropping any of
    # these would break insertion, so pin them here.
    assert record['callsign'] == 'SWR123'
    assert record['prev_contact'] == T0
    assert record['last_contact'] == T0 + timedelta(seconds=10)


def test_decimal_gap_from_sql_is_handled():
    # EXTRACT(EPOCH ...) comes back from PostgreSQL as Decimal.
    records = build_anomaly_records(
        [make_row(gap=Decimal('10.0'))], threshold=2.0)
    assert records[0]['implied_accel'] == 6.37


def test_zero_or_negative_gap_skipped():
    assert build_anomaly_records([make_row(gap=0)], 2.0) == []
    assert build_anomaly_records([make_row(gap=-5)], 2.0) == []
    assert build_anomaly_records([make_row(gap=None)], 2.0) == []


def test_mixed_batch_skips_invalid_and_preserves_order():
    # A bad row in the middle must not drop the valid rows around it, and
    # order must be preserved (guards against an early-return regression).
    rows = [
        make_row(prev_v=100.0, new_v=170.0),   # valid, delta 70
        make_row(gap=0),                        # invalid gap, skipped
        make_row(prev_v=232.0, new_v=168.3),    # valid, delta 63.7
    ]
    records = build_anomaly_records(rows, threshold=2.0)
    assert [r['delta_v'] for r in records] == [70.0, 63.7]


def test_threshold_is_recorded_not_filtered():
    # build_anomaly_records does not gate on the threshold — the SQL candidate
    # query does. A sub-threshold row (implied accel 1.0 < 2.0) still yields a
    # record carrying the threshold for the explanation string.
    records = build_anomaly_records(
        [make_row(prev_v=100.0, new_v=110.0)], threshold=2.0)
    assert len(records) == 1
    assert records[0]['delta_v'] == 10.0
    assert records[0]['implied_accel'] == 1.0
    assert records[0]['threshold'] == 2.0


def test_summary_describes_implied_acceleration():
    records = build_anomaly_records([make_row()], threshold=2.0)
    assert format_summary(records[0]) == (
        'Implied acceleration 6.4 m/s² over 10s exceeds the '
        '2.0 m/s² threshold')


def test_summary_wording_is_direction_agnostic():
    # A speed decrease reads the same as an increase: detection is about the
    # magnitude of the implied acceleration, not its sign.
    records = build_anomaly_records(
        [make_row(prev_v=232.0, new_v=168.3)], threshold=2.0)
    assert format_summary(records[0]) == (
        'Implied acceleration 6.4 m/s² over 10s exceeds the '
        '2.0 m/s² threshold')
