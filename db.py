"""Shared PostgreSQL helpers: connections, startup wait, schema migrations."""

import logging
import time
from pathlib import Path

import psycopg2
import psycopg2.pool

import config

logger = logging.getLogger(__name__)

_MIGRATIONS_PATH = Path(__file__).resolve().parent / 'migrations.sql'
# Arbitrary advisory-lock key shared by all services so concurrent startups
# apply migrations one at a time.
_MIGRATION_LOCK_ID = 74123


def connection_kwargs() -> dict:
    return {
        'host': config.DB_HOST,
        'port': config.DB_PORT,
        'dbname': config.DB_NAME,
        'user': config.DB_USER,
        'password': config.DB_PASSWORD,
        'connect_timeout': config.DB_CONNECT_TIMEOUT_SECONDS,
        'keepalives': 1,
        'keepalives_idle': 30,
        'keepalives_interval': 10,
        'keepalives_count': 3,
    }


def connect() -> psycopg2.extensions.connection:
    return psycopg2.connect(**connection_kwargs())


def create_pool(minconn: int = 1,
                maxconn: int = 5) -> psycopg2.pool.ThreadedConnectionPool:
    return psycopg2.pool.ThreadedConnectionPool(
        minconn, maxconn, **connection_kwargs())


def wait_for_db(max_retries: int = 30, delay_seconds: float = 2.0) -> None:
    """Block until the database accepts connections.

    Raises RuntimeError if it never comes up, so callers can exit non-zero
    and let the container restart policy take over.
    """
    for attempt in range(1, max_retries + 1):
        try:
            connect().close()
            logger.info('Database connection established')
            return
        except psycopg2.Error as exc:
            logger.info('Waiting for database (%d/%d): %s',
                        attempt, max_retries, exc)
            time.sleep(delay_seconds)
    raise RuntimeError(
        f'database not reachable after {max_retries} attempts')


def apply_migrations() -> None:
    """Apply the idempotent schema migrations under an advisory lock."""
    sql = _MIGRATIONS_PATH.read_text()
    conn = connect()
    try:
        with conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    'SELECT pg_advisory_xact_lock(%s)', (_MIGRATION_LOCK_ID,))
                cursor.execute(sql)
    finally:
        conn.close()
    logger.info('Schema migrations applied')
