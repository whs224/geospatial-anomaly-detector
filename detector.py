#!/usr/bin/env python3
"""
Real-Time Geospatial Anomaly Detector - Anomaly Detection Script
Detects erratic velocity changes in flight data
"""

import os
import time
import psycopg2
from datetime import datetime
from typing import List, Tuple, Optional

# Database connection parameters
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('DB_NAME', 'geospatial_db')
DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASSWORD = os.getenv('DB_PASSWORD', 'postgres')

# Detection parameters
VELOCITY_CHANGE_THRESHOLD = 30.0  # m/s
DETECTION_INTERVAL = 10  # seconds


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


def detect_velocity_anomalies(conn: psycopg2.extensions.connection) -> List[Tuple[str, str, float]]:
    """
    Detect flights with erratic velocity changes (> 30 m/s) between their two most recent updates.
    
    Uses LAG() window function to compare current velocity with previous velocity for each flight.
    
    Returns:
        List of tuples: (icao24, callsign, velocity_delta)
    """
    cursor = conn.cursor()
    anomalies = []
    
    try:
        # Query to find velocity anomalies using LAG() window function
        query = """
            WITH velocity_changes AS (
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
                FROM flight_positions
                WHERE velocity IS NOT NULL
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
            SELECT 
                icao24,
                COALESCE(callsign, 'UNKNOWN') AS callsign,
                velocity_delta
            FROM latest_positions
            WHERE rn = 1
            ORDER BY velocity_delta DESC;
        """
        
        cursor.execute(query, (VELOCITY_CHANGE_THRESHOLD,))
        results = cursor.fetchall()
        
        for row in results:
            icao24, callsign, delta = row
            anomalies.append((icao24, callsign, float(delta)))
        
    except psycopg2.Error as e:
        print(f"Database error during anomaly detection: {e}")
    finally:
        cursor.close()
    
    return anomalies


def detection_cycle():
    """Single cycle of anomaly detection."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"\n[{timestamp}] Running anomaly detection...")
    
    try:
        conn = get_db_connection()
        anomalies = detect_velocity_anomalies(conn)
        conn.close()
        
        if anomalies:
            print(f"Found {len(anomalies)} anomaly/anomalies:")
            for icao24, callsign, delta in anomalies:
                print(f"[ANOMALY DETECTED] Flight {callsign} changed speed by {delta:.2f} m/s")
        else:
            print("No anomalies detected")
            
    except Exception as e:
        print(f"Error in detection cycle: {e}")


def main():
    """Main loop that runs anomaly detection every 10 seconds."""
    print("=" * 60)
    print("Real-Time Geospatial Anomaly Detector - Detection Service")
    print("=" * 60)
    print(f"Database: {DB_HOST}:{DB_PORT}/{DB_NAME}")
    print(f"Detection interval: {DETECTION_INTERVAL} seconds")
    print(f"Velocity change threshold: {VELOCITY_CHANGE_THRESHOLD} m/s")
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
    
    # Wait a bit for initial data to be ingested
    print("\nWaiting for initial data ingestion...")
    time.sleep(15)
    
    # Main detection loop
    print(f"\nStarting detection loop (every {DETECTION_INTERVAL} seconds)...")
    print("Press Ctrl+C to stop\n")
    
    try:
        while True:
            detection_cycle()
            time.sleep(DETECTION_INTERVAL)
    except KeyboardInterrupt:
        print("\n\nDetection stopped by user")
    except Exception as e:
        print(f"\nFatal error: {e}")
        raise


if __name__ == "__main__":
    main()

