#!/usr/bin/env python3
"""Anomaly detection service.

Every cycle, compares consecutive observations per aircraft inside a recent
window. A velocity change above the threshold between two observations close
enough in time to be comparable is persisted to anomaly_events with its full
kinematic evidence; the unique key makes re-detection across cycles a no-op.
"""

import logging
import sys
from typing import Any, Dict, List

import psycopg2
from psycopg2.extras import execute_values

import config
import db
import runtime
from anomaly import build_anomaly_records, format_summary

logger = logging.getLogger('detector')

# Consecutive-observation pairs whose velocity delta exceeds the threshold.
# The max-gap predicate keeps unrelated observations (an aircraft leaving the
# bounding box and returning much later) from being compared.
CANDIDATE_QUERY = """
    WITH recent AS (
        SELECT icao24, callsign, velocity, last_contact
        FROM flight_positions
        WHERE last_contact >= now() - make_interval(secs => %(lookback)s)
          AND velocity IS NOT NULL
    ),
    pairs AS (
        SELECT
            icao24,
            callsign,
            velocity AS new_velocity,
            last_contact,
            LAG(velocity) OVER w AS prev_velocity,
            LAG(last_contact) OVER w AS prev_contact
        FROM recent
        WINDOW w AS (PARTITION BY icao24 ORDER BY last_contact)
    )
    SELECT
        icao24,
        callsign,
        prev_velocity,
        new_velocity,
        prev_contact,
        last_contact,
        EXTRACT(EPOCH FROM (last_contact - prev_contact)) AS time_gap_seconds
    FROM pairs
    WHERE prev_velocity IS NOT NULL
      AND ABS(new_velocity - prev_velocity) > %(threshold)s
      AND last_contact - prev_contact <= make_interval(secs => %(max_gap)s)
    ORDER BY icao24, last_contact
"""

INSERT_QUERY = """
    INSERT INTO anomaly_events
        (icao24, callsign, prev_velocity, new_velocity, delta_v,
         time_gap_seconds, implied_accel, threshold, prev_contact,
         last_contact)
    VALUES %s
    ON CONFLICT (icao24, last_contact) DO NOTHING
    RETURNING icao24, last_contact
"""


def fetch_candidates(conn: psycopg2.extensions.connection) -> List[tuple]:
    with conn.cursor() as cursor:
        cursor.execute(CANDIDATE_QUERY, {
            'lookback': config.DETECTION_LOOKBACK_SECONDS,
            'threshold': config.VELOCITY_CHANGE_THRESHOLD_MS,
            'max_gap': config.MAX_TIME_GAP_SECONDS,
        })
        return cursor.fetchall()


def persist_anomalies(conn: psycopg2.extensions.connection,
                      records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Insert evidence records; returns only the ones that were new."""
    if not records:
        return []
    rows = [
        (r['icao24'], r['callsign'], r['prev_velocity'], r['new_velocity'],
         r['delta_v'], r['time_gap_seconds'], r['implied_accel'],
         r['threshold'], r['prev_contact'], r['last_contact'])
        for r in records
    ]
    with conn.cursor() as cursor:
        returned = execute_values(
            cursor, INSERT_QUERY, rows, page_size=200, fetch=True)
    new_keys = set(returned)
    return [r for r in records
            if (r['icao24'], r['last_contact']) in new_keys]


def detection_cycle(conn: psycopg2.extensions.connection) -> None:
    candidates = fetch_candidates(conn)
    records = build_anomaly_records(
        candidates, config.VELOCITY_CHANGE_THRESHOLD_MS)
    new_records = persist_anomalies(conn, records)
    # Commit even on read-only cycles: an open transaction would freeze
    # now() in the candidate query and leave the session idle-in-transaction.
    conn.commit()
    for record in new_records:
        logger.warning(
            '[ANOMALY] %s (%s): %s',
            record['callsign'] or 'UNKNOWN', record['icao24'],
            format_summary(record))
    if not new_records:
        logger.info('No new anomalies (%d candidate pairs re-checked)',
                    len(records))


def run_loop() -> None:
    conn = None
    while True:
        runtime.heartbeat()
        try:
            if conn is None or conn.closed:
                conn = db.connect()
            detection_cycle(conn)
        except psycopg2.Error as exc:
            if conn is not None:
                try:
                    conn.close()
                except psycopg2.Error:
                    pass
                conn = None
            logger.error('Database error (%s); reconnecting next cycle', exc)
        runtime.sleep_with_heartbeat(config.DETECTION_INTERVAL_SECONDS)


def main() -> None:
    config.setup_logging()
    logger.info(
        'Starting detection: threshold=%.1f m/s, max gap=%.0fs, '
        'lookback=%.0fs, interval=%ds',
        config.VELOCITY_CHANGE_THRESHOLD_MS, config.MAX_TIME_GAP_SECONDS,
        config.DETECTION_LOOKBACK_SECONDS, config.DETECTION_INTERVAL_SECONDS)

    try:
        db.wait_for_db()
        db.apply_migrations()
    except (RuntimeError, psycopg2.Error, OSError) as exc:
        logger.critical('Startup failed: %s', exc)
        sys.exit(1)

    runtime.install_sigterm_handler()
    try:
        run_loop()
    except KeyboardInterrupt:
        logger.info('Interrupted; shutting down')


if __name__ == '__main__':
    main()
