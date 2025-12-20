#!/usr/bin/env python3
"""
Real-Time Geospatial Anomaly Detector - Data Ingestion Script
Fetches flight data from OpenSky Network API and stores it in PostgreSQL with PostGIS
"""

import os
import time
import psycopg2
import requests
from datetime import datetime
from typing import Optional, List, Dict, Any

# Database connection parameters
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('DB_NAME', 'geospatial_db')
DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASSWORD = os.getenv('DB_PASSWORD', 'postgres')

# OpenSky Network API endpoint
OPENSKY_API_URL = "https://opensky-network.org/api/states/all"

# Switzerland bounding box
SWITZERLAND_BBOX = {
    'lamin': 45.8,
    'lomin': 5.9,
    'lamax': 47.8,
    'lomax': 10.5
}

# Fetch interval in seconds
FETCH_INTERVAL = 10


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
        print(f"Error connecting to database: {e}")
        raise


def fetch_flight_data() -> Optional[List[List[Any]]]:
    """
    Fetch flight data from OpenSky Network API for Switzerland region.
    
    Returns:
        List of flight state vectors or None if request fails
    """
    try:
        params = {
            'lamin': SWITZERLAND_BBOX['lamin'],
            'lomin': SWITZERLAND_BBOX['lomin'],
            'lamax': SWITZERLAND_BBOX['lamax'],
            'lomax': SWITZERLAND_BBOX['lomax']
        }
        
        response = requests.get(OPENSKY_API_URL, params=params, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        
        if 'states' in data and data['states']:
            return data['states']
        else:
            print("No flight data returned from API")
            return []
            
    except requests.exceptions.RequestException as e:
        print(f"Error fetching data from OpenSky API: {e}")
        return None
    except Exception as e:
        print(f"Unexpected error fetching flight data: {e}")
        return None


def parse_flight_state(state_vector: List[Any]) -> Optional[Dict[str, Any]]:
    """
    Parse OpenSky Network state vector into structured format.
    
    OpenSky state vector format:
    [0] icao24, [1] callsign, [2] origin_country, [3] time_position,
    [4] last_contact, [5] longitude, [6] latitude, [7] baro_altitude,
    [8] on_ground, [9] velocity, [10] heading, [11] vertical_rate,
    [12] sensors, [13] geo_altitude, [14] squawk, [15] spi, [16] position_source
    """
    try:
        # Extract fields - handle None values
        icao24 = state_vector[0] if state_vector[0] else None
        callsign = state_vector[1].strip() if state_vector[1] else None
        last_contact = state_vector[4]
        longitude = state_vector[5]
        latitude = state_vector[6]
        velocity = state_vector[9]
        heading = state_vector[10]
        
        # Critical: Filter out rows with null latitude/longitude
        if latitude is None or longitude is None:
            return None
        
        # Convert last_contact from Unix timestamp to datetime
        if last_contact:
            last_contact_dt = datetime.fromtimestamp(last_contact)
        else:
            last_contact_dt = datetime.now()
        
        return {
            'icao24': icao24,
            'callsign': callsign,
            'velocity': velocity,
            'heading': heading,
            'last_contact': last_contact_dt,
            'longitude': longitude,
            'latitude': latitude
        }
    except (IndexError, ValueError, TypeError) as e:
        print(f"Error parsing flight state: {e}")
        return None


def insert_flight_data(conn: psycopg2.extensions.connection, flight_data: List[Dict[str, Any]]) -> int:
    """
    Insert flight data into PostgreSQL using PostGIS.
    
    Args:
        conn: Database connection
        flight_data: List of parsed flight data dictionaries
        
    Returns:
        Number of records inserted
    """
    if not flight_data:
        return 0
    
    cursor = conn.cursor()
    inserted_count = 0
    
    try:
        insert_query = """
            INSERT INTO flight_positions (icao24, callsign, velocity, heading, last_contact, geom)
            VALUES (%s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
        """
        
        for flight in flight_data:
            try:
                cursor.execute(
                    insert_query,
                    (
                        flight['icao24'],
                        flight['callsign'],
                        flight['velocity'],
                        flight['heading'],
                        flight['last_contact'],
                        flight['longitude'],
                        flight['latitude']
                    )
                )
                inserted_count += 1
            except psycopg2.Error as e:
                print(f"Error inserting flight data: {e}")
                continue
        
        conn.commit()
        print(f"Successfully inserted {inserted_count} flight positions")
        
    except psycopg2.Error as e:
        conn.rollback()
        print(f"Database error during insert: {e}")
    finally:
        cursor.close()
    
    return inserted_count


def ingest_cycle():
    """Single cycle of data ingestion: fetch, parse, and insert."""
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting ingestion cycle...")
    
    # Fetch data from API
    states = fetch_flight_data()
    
    if states is None:
        print("Failed to fetch data, skipping this cycle")
        return
    
    if not states:
        print("No flight data available")
        return
    
    # Parse and filter flight data
    parsed_flights = []
    for state in states:
        parsed = parse_flight_state(state)
        if parsed:  # Only include non-null lat/lon flights
            parsed_flights.append(parsed)
    
    print(f"Parsed {len(parsed_flights)} valid flight positions (filtered {len(states) - len(parsed_flights)} with null coordinates)")
    
    if not parsed_flights:
        print("No valid flight positions to insert")
        return
    
    # Insert into database
    try:
        conn = get_db_connection()
        inserted = insert_flight_data(conn, parsed_flights)
        conn.close()
    except Exception as e:
        print(f"Error in database operation: {e}")


def main():
    """Main loop that runs ingestion every 10 seconds."""
    print("=" * 60)
    print("Real-Time Geospatial Anomaly Detector - Data Ingestion")
    print("=" * 60)
    print(f"Database: {DB_HOST}:{DB_PORT}/{DB_NAME}")
    print(f"Fetch interval: {FETCH_INTERVAL} seconds")
    print(f"Target region: Switzerland (bbox: {SWITZERLAND_BBOX})")
    print("=" * 60)
    
    # Wait for database to be ready
    print("Waiting for database connection...")
    max_retries = 30
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            conn = get_db_connection()
            conn.close()
            print("Database connection established!")
            break
        except psycopg2.Error:
            retry_count += 1
            print(f"Retrying database connection ({retry_count}/{max_retries})...")
            time.sleep(2)
    else:
        print("Failed to connect to database after maximum retries")
        return
    
    # Main ingestion loop
    print(f"\nStarting ingestion loop (every {FETCH_INTERVAL} seconds)...")
    print("Press Ctrl+C to stop\n")
    
    try:
        while True:
            ingest_cycle()
            time.sleep(FETCH_INTERVAL)
    except KeyboardInterrupt:
        print("\n\nIngestion stopped by user")
    except Exception as e:
        print(f"\nFatal error: {e}")
        raise


if __name__ == "__main__":
    main()

