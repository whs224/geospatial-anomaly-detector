"""Pure anomaly-evidence logic shared by the detector and the API.

Kept free of database and framework imports so it is trivially unit-testable.
"""

from typing import Any, Dict, List, Sequence

# Row shape produced by the detector's candidate query:
# (icao24, callsign, prev_velocity, new_velocity, prev_contact, last_contact,
#  time_gap_seconds)


def build_anomaly_records(rows: Sequence[Sequence[Any]],
                          threshold: float) -> List[Dict[str, Any]]:
    """Turn candidate query rows into evidence records with the implied
    acceleration filled in. Rows with a non-positive time gap are skipped —
    they cannot yield a meaningful acceleration."""
    records: List[Dict[str, Any]] = []
    for icao24, callsign, prev_v, new_v, prev_contact, last_contact, gap in rows:
        if gap is None:
            continue
        gap_seconds = float(gap)
        if gap_seconds <= 0:
            continue
        prev_velocity = float(prev_v)
        new_velocity = float(new_v)
        delta_v = abs(new_velocity - prev_velocity)
        records.append({
            'icao24': icao24,
            'callsign': callsign,
            'prev_velocity': prev_velocity,
            'new_velocity': new_velocity,
            # Rounded: sub-cm/s precision is float noise, not evidence.
            'delta_v': round(delta_v, 2),
            'time_gap_seconds': gap_seconds,
            'implied_accel': round(delta_v / gap_seconds, 2),
            'threshold': float(threshold),
            'prev_contact': prev_contact,
            'last_contact': last_contact,
        })
    return records


def format_summary(record: Dict[str, Any]) -> str:
    """One-line human-readable explanation of an anomaly event."""
    verb = ('jumped' if record['new_velocity'] >= record['prev_velocity']
            else 'dropped')
    return (
        f"Speed {verb} {record['delta_v']:.1f} m/s in "
        f"{record['time_gap_seconds']:.0f}s "
        f"({record['implied_accel']:.1f} m/s² implied), "
        f"exceeding the {record['threshold']:.0f} m/s threshold")
