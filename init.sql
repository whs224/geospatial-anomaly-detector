-- Enable PostGIS extension
CREATE EXTENSION IF NOT EXISTS postgis;

-- Create flight_positions table
CREATE TABLE IF NOT EXISTS flight_positions (
    id SERIAL PRIMARY KEY,
    icao24 VARCHAR(10) NOT NULL,
    callsign VARCHAR(20),
    velocity FLOAT,
    heading FLOAT,
    last_contact TIMESTAMP NOT NULL,
    geom GEOMETRY(Point, 4326) NOT NULL
);

-- Create spatial index on geom column for performance
CREATE INDEX IF NOT EXISTS idx_flight_positions_geom 
ON flight_positions USING GIST (geom);

-- Create index on icao24 for faster lookups
CREATE INDEX IF NOT EXISTS idx_flight_positions_icao24 
ON flight_positions (icao24);

-- Create index on last_contact for time-based queries
CREATE INDEX IF NOT EXISTS idx_flight_positions_last_contact 
ON flight_positions (last_contact);

