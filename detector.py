#!/usr/bin/env python3
"""Anomaly detection service.

Runs on an asyncio event loop. Every cycle it awaits a windowed comparison of
consecutive observations per aircraft and awaits the writes for any new anomaly.
An implied acceleration (velocity change over the time gap) above the threshold,
between two observations close enough in time to be comparable, is persisted to
anomaly_events with its full kinematic evidence; the unique key makes
re-detection across cycles a no-op. Database access uses the async psycopg
driver, so awaiting a query yields the event loop instead of blocking a thread.
"""

import asyncio
import logging
import signal
import sys
import time
from typing import Any, Dict, List

import psycopg

import config
import db
import runtime
from anomaly import build_anomaly_records, format_summary

logger = logging.getLogger('detector')

# Sub-tick used while awaiting the inter-cycle sleep, so the heartbeat stays
# fresh and a SIGTERM is honored promptly.
_HEARTBEAT_TICK_SECONDS = 15.0

# Consecutive-observation pairs whose implied acceleration (velocity delta over
# the time gap) exceeds the threshold. The max-gap predicate keeps unrelated
# observations (an aircraft leaving the bounding box and returning much later)
# from being compared.
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
      AND last_contact > prev_contact
      AND last_contact - prev_contact <= make_interval(secs => %(max_gap)s)
      AND ABS(new_velocity - prev_velocity)
          / EXTRACT(EPOCH FROM (last_contact - prev_contact))
          > %(accel_threshold)s
    ORDER BY icao24, last_contact
"""

# One row per candidate; ON CONFLICT DO NOTHING makes re-detection idempotent,
# and the RETURNING row tells us which events were genuinely new this cycle.
INSERT_QUERY = """
    INSERT INTO anomaly_events
        (icao24, callsign, prev_velocity, new_velocity, delta_v,
         time_gap_seconds, implied_accel, threshold, prev_contact,
         last_contact)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (icao24, last_contact) DO NOTHING
    RETURNING icao24, last_contact
"""


async def fetch_candidates(conn: psycopg.AsyncConnection) -> List[tuple]:
    async with conn.cursor() as cursor:
        await cursor.execute(CANDIDATE_QUERY, {
            'lookback': config.DETECTION_LOOKBACK_SECONDS,
            'accel_threshold': config.ACCEL_THRESHOLD_MS2,
            'max_gap': config.MAX_TIME_GAP_SECONDS,
        })
        return await cursor.fetchall()


async def persist_anomalies(conn: psycopg.AsyncConnection,
                            records: List[Dict[str, Any]]
                            ) -> List[Dict[str, Any]]:
    """Insert evidence records; returns only the ones that were new.

    Anomaly counts per cycle are small, so a per-row insert is cheap, and the
    ON CONFLICT DO NOTHING ... RETURNING keeps the idempotency and the
    new-vs-already-seen distinction identical to the previous batch insert.
    """
    if not records:
        return []
    new_records: List[Dict[str, Any]] = []
    async with conn.cursor() as cursor:
        for r in records:
            await cursor.execute(INSERT_QUERY, (
                r['icao24'], r['callsign'], r['prev_velocity'],
                r['new_velocity'], r['delta_v'], r['time_gap_seconds'],
                r['implied_accel'], r['threshold'], r['prev_contact'],
                r['last_contact']))
            if await cursor.fetchone() is not None:
                new_records.append(r)
    return new_records


async def detection_cycle(conn: psycopg.AsyncConnection) -> None:
    candidates = await fetch_candidates(conn)
    records = build_anomaly_records(
        candidates, config.ACCEL_THRESHOLD_MS2)
    new_records = await persist_anomalies(conn, records)
    # Commit even on read-only cycles: an open transaction would freeze
    # now() in the candidate query and leave the session idle-in-transaction.
    await conn.commit()
    for record in new_records:
        logger.warning(
            '[ANOMALY] %s (%s): %s',
            record['callsign'] or 'UNKNOWN', record['icao24'],
            format_summary(record))
    if not new_records:
        logger.info('No new anomalies (%d candidate pairs re-checked)',
                    len(records))


async def _connect() -> psycopg.AsyncConnection:
    """Open an async connection using the same libpq settings as the sync
    services (host, credentials, keepalives, connect timeout)."""
    return await psycopg.AsyncConnection.connect(**db.connection_kwargs())


async def _sleep_with_heartbeat(seconds: float, stop: asyncio.Event) -> None:
    """Await the inter-cycle delay in short ticks, refreshing the heartbeat and
    returning immediately once SIGTERM has set the stop event."""
    deadline = time.monotonic() + seconds
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        runtime.heartbeat()
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=min(_HEARTBEAT_TICK_SECONDS, remaining))
        except asyncio.TimeoutError:
            continue  # tick elapsed; loop to refresh the heartbeat
        return  # stop was set — shut down promptly


async def run_loop(stop: asyncio.Event) -> None:
    conn: psycopg.AsyncConnection = None
    while not stop.is_set():
        runtime.heartbeat()
        try:
            if conn is None or conn.closed:
                conn = await _connect()
            await detection_cycle(conn)
        except psycopg.Error as exc:
            if conn is not None:
                try:
                    await conn.close()
                except psycopg.Error:
                    pass
                conn = None
            logger.error('Database error (%s); reconnecting next cycle', exc)
        await _sleep_with_heartbeat(config.DETECTION_INTERVAL_SECONDS, stop)
    if conn is not None and not conn.closed:
        await conn.close()


async def _wait_for_db(max_retries: int = 30,
                       delay_seconds: float = 2.0) -> None:
    """Block until the database accepts connections, over the async driver."""
    for attempt in range(1, max_retries + 1):
        try:
            conn = await _connect()
            await conn.close()
            logger.info('Database connection established')
            return
        except psycopg.Error as exc:
            logger.info('Waiting for database (%d/%d): %s',
                        attempt, max_retries, exc)
            await asyncio.sleep(delay_seconds)
    raise RuntimeError(
        f'database not reachable after {max_retries} attempts')


async def _apply_migrations() -> None:
    """Apply the idempotent migrations under the same advisory lock as the sync
    services, but over the async driver, so concurrent startups still serialize
    and fresh/long-running databases converge on the same schema."""
    sql = db._MIGRATIONS_PATH.read_text()
    conn = await _connect()
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(
                'SELECT pg_advisory_xact_lock(%s)', (db._MIGRATION_LOCK_ID,))
            await cursor.execute(sql)
        await conn.commit()
    finally:
        await conn.close()
    logger.info('Schema migrations applied')


async def _amain() -> None:
    try:
        await _wait_for_db()
        await _apply_migrations()
    except (RuntimeError, psycopg.Error, OSError) as exc:
        logger.critical('Startup failed: %s', exc)
        sys.exit(1)

    stop = asyncio.Event()
    # asyncio-native SIGTERM handling: set the stop event so `docker stop`
    # drains the current cycle and exits cleanly instead of via SIGKILL.
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    await run_loop(stop)


def main() -> None:
    config.setup_logging()
    logger.info(
        'Starting detection (asyncio): accel threshold=%.1f m/s², '
        'max gap=%.0fs, lookback=%.0fs, interval=%ds',
        config.ACCEL_THRESHOLD_MS2, config.MAX_TIME_GAP_SECONDS,
        config.DETECTION_LOOKBACK_SECONDS, config.DETECTION_INTERVAL_SECONDS)

    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        logger.info('Interrupted; shutting down')


if __name__ == '__main__':
    main()
