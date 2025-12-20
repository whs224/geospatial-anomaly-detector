#!/usr/bin/env python3
"""
FastAPI server for Real-Time Geospatial Anomaly Detector
Provides GeoJSON endpoint for flight visualization
"""

import os
import psycopg2
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any
from datetime import datetime, timedelta

# Database connection parameters
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('DB_NAME', 'geospatial_db')
DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASSWORD = os.getenv('DB_PASSWORD', 'postgres')

# Anomaly detection threshold
VELOCITY_CHANGE_THRESHOLD = 30.0  # m/s

app = FastAPI(title="Geospatial Anomaly Detector API")

# Enable CORS for local HTML file
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for local development
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db_connection() -> psycopg2.extensions.connection:
    """Establish connection to PostgreSQL database."""
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD
        )
        return conn
    except psycopg2.Error as e:
        raise HTTPException(status_code=500, detail=f"Database connection error: {e}")


def get_anomalous_flights(conn: psycopg2.extensions.connection) -> set:
    """
    Get set of icao24 values for flights with anomalies in the last minute.
    Re-runs the anomaly detection logic.
    
    Returns:
        Set of icao24 strings that are anomalous
    """
    cursor = conn.cursor()
    anomalous_icao24s = set()
    
    try:
        # Query to find velocity anomalies in the last minute
        query = """
            WITH recent_positions AS (
                SELECT *
                FROM flight_positions
                WHERE last_contact >= NOW() - INTERVAL '1 minute'
                    AND velocity IS NOT NULL
            ),
            velocity_changes AS (
                SELECT 
                    icao24,
                    callsign,
                    velocity,
                    last_contact,
                    LAG(velocity) OVER (
                        PARTITION BY icao24 
                        ORDER BY last_contact
                    ) AS previous_velocity,
                    ABS(velocity - LAG(velocity) OVER (
                        PARTITION BY icao24 
                        ORDER BY last_contact
                    )) AS velocity_delta
                FROM recent_positions
            ),
            latest_positions AS (
                SELECT 
                    icao24,
                    callsign,
                    velocity_delta,
                    ROW_NUMBER() OVER (
                        PARTITION BY icao24 
                        ORDER BY last_contact DESC
                    ) AS rn
                FROM velocity_changes
                WHERE previous_velocity IS NOT NULL
                    AND velocity_delta > %s
            )
            SELECT DISTINCT icao24
            FROM latest_positions
            WHERE rn = 1;
        """
        
        cursor.execute(query, (VELOCITY_CHANGE_THRESHOLD,))
        results = cursor.fetchall()
        
        for row in results:
            anomalous_icao24s.add(row[0])
        
    except psycopg2.Error as e:
        print(f"Error detecting anomalies: {e}")
    finally:
        cursor.close()
    
    return anomalous_icao24s


@app.get("/flights")
async def get_flights() -> Dict[str, Any]:
    """
    Get latest position of all flights as GeoJSON FeatureCollection.
    Flags flights with anomalies in the last minute.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get anomalous flights
        anomalous_flights = get_anomalous_flights(conn)
        
        # Get latest position for each flight
        query = """
            WITH latest_positions AS (
                SELECT 
                    icao24,
                    callsign,
                    velocity,
                    heading,
                    last_contact,
                    ST_X(geom) AS longitude,
                    ST_Y(geom) AS latitude,
                    ROW_NUMBER() OVER (
                        PARTITION BY icao24 
                        ORDER BY last_contact DESC
                    ) AS rn
                FROM flight_positions
                WHERE geom IS NOT NULL
            )
            SELECT 
                icao24,
                COALESCE(callsign, 'UNKNOWN') AS callsign,
                velocity,
                heading,
                last_contact,
                longitude,
                latitude
            FROM latest_positions
            WHERE rn = 1
            ORDER BY last_contact DESC;
        """
        
        cursor.execute(query)
        rows = cursor.fetchall()
        
        # Build GeoJSON FeatureCollection
        features = []
        for row in rows:
            icao24, callsign, velocity, heading, last_contact, lon, lat = row
            
            # Check if this flight is anomalous
            is_anomaly = icao24 in anomalous_flights
            
            feature = {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [lon, lat]
                },
                "properties": {
                    "icao24": icao24,
                    "callsign": callsign or "UNKNOWN",
                    "velocity": velocity,
                    "heading": heading,
                    "last_contact": last_contact.isoformat() if last_contact else None,
                    "is_anomaly": is_anomaly
                }
            }
            features.append(feature)
        
        geojson = {
            "type": "FeatureCollection",
            "features": features
        }
        
        cursor.close()
        return geojson
        
    except psycopg2.Error as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")
    finally:
        if conn:
            conn.close()


@app.get("/")
async def root():
    """Health check endpoint."""
    return {"status": "ok", "service": "Geospatial Anomaly Detector API"}

