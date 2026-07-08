-- Idempotent schema migrations, applied by the ingestor and detector at
-- startup under an advisory lock. Safe to run repeatedly against a live
-- database; fresh and existing databases converge on the same schema.

CREATE TABLE IF NOT EXISTS flight_positions (
    id BIGSERIAL PRIMARY KEY,
    icao24 VARCHAR(10) NOT NULL,
    callsign VARCHAR(20),
    velocity DOUBLE PRECISION,
    heading DOUBLE PRECISION,
    last_contact TIMESTAMPTZ NOT NULL,
    geom GEOMETRY(Point, 4326) NOT NULL
);

-- Upgrade pre-existing tables in place: widen the id to bigint (the table is
-- append-heavy) and make timestamps timezone-aware (stored values were
-- already UTC).
DO $$
BEGIN
    IF (SELECT data_type FROM information_schema.columns
        WHERE table_name = 'flight_positions' AND column_name = 'id')
        = 'integer' THEN
        ALTER TABLE flight_positions ALTER COLUMN id TYPE BIGINT;
    END IF;

    IF (SELECT data_type FROM information_schema.columns
        WHERE table_name = 'flight_positions' AND column_name = 'last_contact')
        = 'timestamp without time zone' THEN
        ALTER TABLE flight_positions
            ALTER COLUMN last_contact TYPE TIMESTAMPTZ
            USING last_contact AT TIME ZONE 'UTC';
    END IF;
END $$;

ALTER SEQUENCE flight_positions_id_seq AS BIGINT;

-- De-duplicate rows the pre-constraint ingestor re-inserted (OpenSky repeats
-- a state vector when an aircraft has not transmitted since the last poll),
-- then enforce idempotent ingestion. The unique index doubles as the
-- composite index every window query needs.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE indexname = 'uq_flight_positions_icao24_last_contact')
    THEN
        DELETE FROM flight_positions a
        USING flight_positions b
        WHERE a.icao24 = b.icao24
          AND a.last_contact = b.last_contact
          AND a.id > b.id;

        CREATE UNIQUE INDEX uq_flight_positions_icao24_last_contact
            ON flight_positions (icao24, last_contact);
    END IF;
END $$;

-- Redundant now that the composite index leads with icao24.
DROP INDEX IF EXISTS idx_flight_positions_icao24;

CREATE INDEX IF NOT EXISTS idx_flight_positions_last_contact
    ON flight_positions (last_contact);

CREATE INDEX IF NOT EXISTS idx_flight_positions_geom
    ON flight_positions USING GIST (geom);

-- Anomaly evidence: one row per detected event, keyed by the newer
-- observation of the offending pair so re-detection is a no-op.
CREATE TABLE IF NOT EXISTS anomaly_events (
    id BIGSERIAL PRIMARY KEY,
    icao24 VARCHAR(10) NOT NULL,
    callsign VARCHAR(20),
    prev_velocity DOUBLE PRECISION NOT NULL,
    new_velocity DOUBLE PRECISION NOT NULL,
    delta_v DOUBLE PRECISION NOT NULL,
    time_gap_seconds DOUBLE PRECISION NOT NULL,
    implied_accel DOUBLE PRECISION NOT NULL,
    threshold DOUBLE PRECISION NOT NULL,
    prev_contact TIMESTAMPTZ NOT NULL,
    last_contact TIMESTAMPTZ NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_anomaly_events_icao24_last_contact
        UNIQUE (icao24, last_contact)
);

CREATE INDEX IF NOT EXISTS idx_anomaly_events_detected_at
    ON anomaly_events (detected_at);

-- Airspace sectors: approximate TRACON/approach polygons over the coverage
-- area. The API classifies each aircraft into a sector with a GiST-indexed
-- ST_Contains spatial join, so the stored position geometry earns its keep
-- beyond storage. These are real polygons (octagonal approximations of each
-- terminal area's controlled airspace), not bounding boxes.
CREATE TABLE IF NOT EXISTS sectors (
    id BIGSERIAL PRIMARY KEY,
    code VARCHAR(10) NOT NULL UNIQUE,
    name VARCHAR(80) NOT NULL,
    geom GEOMETRY(Polygon, 4326) NOT NULL
);

-- Spatial index that backs the ST_Contains sector lookup.
CREATE INDEX IF NOT EXISTS idx_sectors_geom ON sectors USING GIST (geom);

-- Idempotent seed: the unique code makes re-running the migration a no-op.
INSERT INTO sectors (code, name, geom) VALUES
    ('N90', 'New York TRACON',
     ST_GeomFromText('POLYGON((-73.18 40.75, -73.3909 41.1389, -73.9 41.3, -74.4091 41.1389, -74.62 40.75, -74.4091 40.3611, -73.9 40.2, -73.3909 40.3611, -73.18 40.75))', 4326)),
    ('A90', 'Boston TRACON',
     ST_GeomFromText('POLYGON((-70.41 42.37, -70.5857 42.6882, -71.01 42.82, -71.4343 42.6882, -71.61 42.37, -71.4343 42.0518, -71.01 41.92, -70.5857 42.0518, -70.41 42.37))', 4326)),
    ('PHL', 'Philadelphia TRACON',
     ST_GeomFromText('POLYGON((-74.66 39.87, -74.8299 40.1882, -75.24 40.32, -75.6501 40.1882, -75.82 39.87, -75.6501 39.5518, -75.24 39.42, -74.8299 39.5518, -74.66 39.87))', 4326)),
    ('PCT', 'Potomac TRACON',
     ST_GeomFromText('POLYGON((-76.3 38.95, -76.505 39.3389, -77.0 39.5, -77.495 39.3389, -77.7 38.95, -77.495 38.5611, -77.0 38.4, -76.505 38.5611, -76.3 38.95))', 4326))
ON CONFLICT (code) DO NOTHING;
