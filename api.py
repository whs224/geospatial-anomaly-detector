#!/usr/bin/env python3
"""API service.

Serves the Leaflet map at / and a GeoJSON feed of latest flight positions at
/flights, annotated with the anomaly evidence the detector persisted.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

import psycopg2
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

import config
import db
from anomaly import format_summary

config.setup_logging()
logger = logging.getLogger('api')

INDEX_PATH = Path(__file__).resolve().parent / 'index.html'

# Latest position per currently-active aircraft. The recency filter keeps the
# scan bounded and stops long-departed aircraft from rendering forever.
LATEST_POSITIONS_QUERY = """
    SELECT DISTINCT ON (icao24)
        icao24,
        callsign,
        velocity,
        heading,
        last_contact,
        ST_X(geom) AS longitude,
        ST_Y(geom) AS latitude
    FROM flight_positions
    WHERE last_contact >= now() - make_interval(secs => %(active_window)s)
    ORDER BY icao24, last_contact DESC
"""

# Most recent anomaly event per aircraft inside the display window.
RECENT_ANOMALIES_QUERY = """
    SELECT DISTINCT ON (icao24)
        icao24,
        callsign,
        prev_velocity,
        new_velocity,
        delta_v,
        time_gap_seconds,
        implied_accel,
        threshold,
        detected_at
    FROM anomaly_events
    WHERE detected_at >= now() - make_interval(secs => %(ttl)s)
    -- last_contact breaks ties: a detector batch inserts every event of a
    -- cycle with the same detected_at, so order by the observation time too.
    ORDER BY icao24, detected_at DESC, last_contact DESC
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Apply migrations here too (under the shared advisory lock) so the API
    # never races the loop services and can serve against a fresh database on
    # its own.
    db.wait_for_db()
    db.apply_migrations()
    app.state.pool = db.create_pool(minconn=1, maxconn=5)
    logger.info('Connection pool ready')
    yield
    app.state.pool.closeall()


app = FastAPI(title='Geospatial Anomaly Detector API', lifespan=lifespan)

# The map is served same-origin; the permissive read-only policy just keeps
# an index.html opened straight from disk working too.
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=False,
    allow_methods=['GET'],
    allow_headers=['*'],
)


def _anomaly_payload(row: tuple) -> Dict[str, Any]:
    (_icao24, _callsign, prev_velocity, new_velocity, delta_v,
     time_gap_seconds, implied_accel, threshold, detected_at) = row
    record = {
        'prev_velocity': prev_velocity,
        'new_velocity': new_velocity,
        'delta_v': delta_v,
        'time_gap_seconds': time_gap_seconds,
        'implied_accel': implied_accel,
        'threshold': threshold,
    }
    return {
        **record,
        'detected_at': detected_at.isoformat(),
        'summary': format_summary(record),
    }


@app.get('/flights')
def get_flights(request: Request) -> Dict[str, Any]:
    """Latest position of active flights as a GeoJSON FeatureCollection.

    Flights with a recent anomaly event carry `is_anomaly: true` plus the
    full evidence payload under `anomaly`.
    """
    pool = request.app.state.pool
    try:
        conn = pool.getconn()
    except psycopg2.Error as exc:
        logger.error('Could not get database connection: %s', exc)
        raise HTTPException(status_code=503, detail='database unavailable')

    try:
        with conn.cursor() as cursor:
            cursor.execute(RECENT_ANOMALIES_QUERY,
                           {'ttl': config.ANOMALY_TTL_SECONDS})
            anomalies = {row[0]: row for row in cursor.fetchall()}
            cursor.execute(LATEST_POSITIONS_QUERY,
                           {'active_window': config.ACTIVE_WINDOW_SECONDS})
            rows = cursor.fetchall()
        conn.commit()
    except psycopg2.Error as exc:
        try:
            conn.rollback()
        except psycopg2.Error:
            pass
        logger.exception('Database error serving /flights')
        raise HTTPException(status_code=500, detail='database error') from exc
    finally:
        pool.putconn(conn)

    features = []
    for icao24, callsign, velocity, heading, last_contact, lon, lat in rows:
        properties: Dict[str, Any] = {
            'icao24': icao24,
            'callsign': callsign or 'UNKNOWN',
            'velocity': velocity,
            'heading': heading,
            'last_contact': last_contact.isoformat(),
            'is_anomaly': icao24 in anomalies,
        }
        if icao24 in anomalies:
            properties['anomaly'] = _anomaly_payload(anomalies[icao24])
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
            'properties': properties,
        })

    return {'type': 'FeatureCollection', 'features': features}


@app.get('/health')
def health(request: Request) -> Dict[str, str]:
    """Liveness check for the service and its database connectivity."""
    pool = request.app.state.pool
    try:
        conn = pool.getconn()
    except psycopg2.Error:
        raise HTTPException(status_code=503, detail='database unavailable')
    try:
        with conn.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        conn.commit()
    except psycopg2.Error:
        try:
            conn.rollback()
        except psycopg2.Error:
            pass
        raise HTTPException(status_code=503, detail='database unavailable')
    finally:
        pool.putconn(conn)
    return {'status': 'ok', 'service': 'Geospatial Anomaly Detector API'}


@app.get('/', include_in_schema=False)
def root() -> FileResponse:
    return FileResponse(INDEX_PATH, media_type='text/html')
