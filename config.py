"""Shared configuration for all services, sourced from environment variables.

Every tunable in the pipeline lives here so the three services never disagree
on thresholds or windows. Values fall back to defaults suitable for local use.
"""

import logging
import os


def _env_raw(name: str):
    """Return the env value, treating unset or blank as absent so that
    `VAR=` (a common docker-compose default) falls back cleanly."""
    value = os.getenv(name)
    if value is None or value.strip() == '':
        return None
    return value


def _env_str(name: str, default: str) -> str:
    value = _env_raw(name)
    return value if value is not None else default


def _env_int(name: str, default: int) -> int:
    value = _env_raw(name)
    return int(value) if value is not None else default


def _env_float(name: str, default: float) -> float:
    value = _env_raw(name)
    return float(value) if value is not None else default


# --- Database ---------------------------------------------------------------
DB_HOST = _env_str('DB_HOST', 'localhost')
DB_PORT = _env_int('DB_PORT', 5432)
DB_NAME = _env_str('DB_NAME', 'geospatial_db')
DB_USER = _env_str('DB_USER', 'postgres')
DB_PASSWORD = _env_str('DB_PASSWORD', 'postgres')
DB_CONNECT_TIMEOUT_SECONDS = _env_int('DB_CONNECT_TIMEOUT_SECONDS', 10)

# --- OpenSky Network ---------------------------------------------------------
OPENSKY_API_URL = _env_str(
    'OPENSKY_API_URL', 'https://opensky-network.org/api/states/all')
OPENSKY_TOKEN_URL = _env_str(
    'OPENSKY_TOKEN_URL',
    'https://auth.opensky-network.org/auth/realms/opensky-network'
    '/protocol/openid-connect/token')
# Optional OAuth2 client credentials. Anonymous access is limited to roughly
# 400 API credits/day; a free registered account gets 4000.
OPENSKY_CLIENT_ID = _env_str('OPENSKY_CLIENT_ID', '')
OPENSKY_CLIENT_SECRET = _env_str('OPENSKY_CLIENT_SECRET', '')

# --- Ingestion ---------------------------------------------------------------
# Switzerland bounding box (~9 square degrees). At this size each /states/all
# request costs 1 API credit, so the fast fetch cadence stays cheap.
BBOX_LAMIN = _env_float('BBOX_LAMIN', 45.8)
BBOX_LOMIN = _env_float('BBOX_LOMIN', 5.9)
BBOX_LAMAX = _env_float('BBOX_LAMAX', 47.8)
BBOX_LOMAX = _env_float('BBOX_LOMAX', 10.5)

FETCH_INTERVAL_SECONDS = _env_int('FETCH_INTERVAL_SECONDS', 10)
# Cap for exponential backoff after failed or rate-limited fetches.
BACKOFF_MAX_SECONDS = _env_int('BACKOFF_MAX_SECONDS', 900)

# Position history older than this is pruned; no consumer reads past a few
# minutes, so retention only serves debugging and replay.
RETENTION_HOURS = _env_int('RETENTION_HOURS', 24)
PRUNE_INTERVAL_SECONDS = _env_int('PRUNE_INTERVAL_SECONDS', 3600)

# --- Detection ---------------------------------------------------------------
VELOCITY_CHANGE_THRESHOLD_MS = _env_float('VELOCITY_CHANGE_THRESHOLD_MS', 30.0)
DETECTION_INTERVAL_SECONDS = _env_int('DETECTION_INTERVAL_SECONDS', 10)
# Observation pairs further apart than this are not comparable: an aircraft
# that left the bounding box and returned later would otherwise produce a
# false positive from two unrelated cruise segments.
MAX_TIME_GAP_SECONDS = _env_float(
    'MAX_TIME_GAP_SECONDS', 2.5 * FETCH_INTERVAL_SECONDS)
# How far back each detection cycle looks for new observation pairs.
DETECTION_LOOKBACK_SECONDS = _env_float(
    'DETECTION_LOOKBACK_SECONDS', MAX_TIME_GAP_SECONDS + 60)

# --- API ---------------------------------------------------------------------
# A flight is "active" (shown on the map) if heard from within this window.
ACTIVE_WINDOW_SECONDS = _env_int(
    'ACTIVE_WINDOW_SECONDS', 3 * FETCH_INTERVAL_SECONDS)
# How long a detected anomaly keeps a flight highlighted on the map.
ANOMALY_TTL_SECONDS = _env_int('ANOMALY_TTL_SECONDS', 300)

LOG_LEVEL = _env_str('LOG_LEVEL', 'INFO')


def setup_logging() -> None:
    """Configure root logging once per process."""
    logging.basicConfig(
        level=LOG_LEVEL,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S')
