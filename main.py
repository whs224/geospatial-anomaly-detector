#!/usr/bin/env python3
"""Data ingestion service.

Fetches state vectors from the OpenSky Network for the configured bounding
box, batch-inserts them idempotently into PostgreSQL/PostGIS, and prunes
position history past the retention window.
"""

import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import execute_values

import config
import db
import runtime
from opensky import OpenSkyClient, OpenSkyError, RateLimitedError

logger = logging.getLogger('ingestor')

INSERT_QUERY = """
    INSERT INTO flight_positions
        (icao24, callsign, velocity, heading, last_contact, geom)
    VALUES %s
    ON CONFLICT (icao24, last_contact) DO NOTHING
    RETURNING 1
"""
INSERT_TEMPLATE = '(%s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))'

PRUNE_POSITIONS_QUERY = """
    DELETE FROM flight_positions
    WHERE last_contact < now() - make_interval(hours => %s)
"""
PRUNE_EVENTS_QUERY = """
    DELETE FROM anomaly_events
    WHERE detected_at < now() - make_interval(hours => %s)
"""


def parse_flight_state(state_vector: Any) -> Optional[Dict[str, Any]]:
    """Parse one OpenSky state vector into a row dict.

    Returns None for vectors that should not be stored: missing coordinates
    or timestamp, or on-ground traffic (taxiing aircraft and airport ground
    vehicles would pollute both the map and the kinematic detection).

    OpenSky state vector format:
    [0] icao24, [1] callsign, [2] origin_country, [3] time_position,
    [4] last_contact, [5] longitude, [6] latitude, [7] baro_altitude,
    [8] on_ground, [9] velocity, [10] heading, [11] vertical_rate,
    [12] sensors, [13] geo_altitude, [14] squawk, [15] spi,
    [16] position_source
    """
    if not isinstance(state_vector, (list, tuple)) or len(state_vector) < 17:
        return None

    icao24 = state_vector[0]
    raw_callsign = state_vector[1]
    last_contact = state_vector[4]
    longitude = state_vector[5]
    latitude = state_vector[6]
    on_ground = state_vector[8]
    velocity = state_vector[9]
    heading = state_vector[10]

    # icao24 is part of the primary key; an empty string is as useless as None.
    if not icao24 or last_contact is None:
        return None
    if latitude is None or longitude is None:
        return None
    if on_ground:
        return None

    callsign = raw_callsign.strip() if raw_callsign else None
    return {
        'icao24': icao24,
        'callsign': callsign or None,
        'velocity': velocity,
        'heading': heading,
        'last_contact': datetime.fromtimestamp(last_contact, tz=timezone.utc),
        'longitude': longitude,
        'latitude': latitude,
    }


def insert_positions(conn: psycopg2.extensions.connection,
                     flights: List[Dict[str, Any]]) -> int:
    """Batch-insert parsed positions; returns the number of new rows.

    Re-polled state vectors (same icao24 + last_contact) are dropped by the
    unique constraint, so ingestion is idempotent.
    """
    if not flights:
        return 0
    rows = [
        (f['icao24'], f['callsign'], f['velocity'], f['heading'],
         f['last_contact'], f['longitude'], f['latitude'])
        for f in flights
    ]
    with conn.cursor() as cursor:
        inserted = execute_values(
            cursor, INSERT_QUERY, rows, template=INSERT_TEMPLATE,
            page_size=500, fetch=True)
    conn.commit()
    return len(inserted)


def prune_old_rows(conn: psycopg2.extensions.connection) -> tuple:
    """Delete positions and anomaly events past the retention window."""
    with conn.cursor() as cursor:
        cursor.execute(PRUNE_POSITIONS_QUERY, (config.RETENTION_HOURS,))
        positions = cursor.rowcount
        cursor.execute(PRUNE_EVENTS_QUERY, (config.RETENTION_HOURS,))
        events = cursor.rowcount
    conn.commit()
    return positions, events


def run_loop() -> None:
    client = OpenSkyClient()
    conn: Optional[psycopg2.extensions.connection] = None
    backoff = float(config.FETCH_INTERVAL_SECONDS)
    last_prune: Optional[float] = None

    while True:
        runtime.heartbeat()
        delay = float(config.FETCH_INTERVAL_SECONDS)
        try:
            states = client.fetch_states()
            flights = [
                parsed for parsed in map(parse_flight_state, states) if parsed
            ]
            if conn is None or conn.closed:
                conn = db.connect()
            inserted = insert_positions(conn, flights)
            logger.info(
                'Fetched %d state vectors, kept %d airborne with coordinates, '
                'inserted %d new positions',
                len(states), len(flights), inserted)

            if (last_prune is None
                    or time.monotonic() - last_prune
                    >= config.PRUNE_INTERVAL_SECONDS):
                pruned_positions, pruned_events = prune_old_rows(conn)
                last_prune = time.monotonic()
                if pruned_positions or pruned_events:
                    logger.info(
                        'Pruned %d positions and %d anomaly events older '
                        'than %dh', pruned_positions, pruned_events,
                        config.RETENTION_HOURS)

            backoff = float(config.FETCH_INTERVAL_SECONDS)
        except RateLimitedError as exc:
            backoff = min(backoff * 2, config.BACKOFF_MAX_SECONDS)
            delay = max(exc.retry_after or 0.0, backoff)
            hint = ('' if client.authenticated else
                    ' — anonymous access gets ~400 credits/day; set '
                    'OPENSKY_CLIENT_ID/OPENSKY_CLIENT_SECRET for 4000')
            logger.warning('%s; backing off %.0fs%s', exc, delay, hint)
        except OpenSkyError as exc:
            backoff = min(backoff * 2, config.BACKOFF_MAX_SECONDS)
            delay = backoff
            logger.warning('OpenSky fetch failed (%s); retrying in %.0fs',
                           exc, delay)
        except psycopg2.Error as exc:
            if conn is not None:
                try:
                    conn.close()
                except psycopg2.Error:
                    pass
                conn = None
            backoff = min(backoff * 2, config.BACKOFF_MAX_SECONDS)
            delay = backoff
            logger.error('Database error (%s); reconnecting in %.0fs',
                         exc, delay)

        runtime.sleep_with_heartbeat(delay)


def main() -> None:
    config.setup_logging()
    auth_mode = ('OAuth2 client credentials'
                 if config.OPENSKY_CLIENT_ID and config.OPENSKY_CLIENT_SECRET
                 else 'anonymous')
    logger.info(
        'Starting ingestion: bbox=(%.1f, %.1f)-(%.1f, %.1f), interval=%ds, '
        'retention=%dh, OpenSky auth=%s',
        config.BBOX_LAMIN, config.BBOX_LOMIN, config.BBOX_LAMAX,
        config.BBOX_LOMAX, config.FETCH_INTERVAL_SECONDS,
        config.RETENTION_HOURS, auth_mode)

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
