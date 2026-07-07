# Real-Time Geospatial Anomaly Detector

A distributed intelligence pipeline that ingests live flight telemetry over Switzerland, detects kinematic anomalies in real time, and visualizes high-priority targets — with the evidence behind every alert — for operator review.

## 🎯 Project Goal
To move beyond static data analysis and build a **living system** that can:
1.  **Ingest** messy, high-velocity data streams (OpenSky Network API) with rate-limit-aware backoff and idempotent writes.
2.  **Decompose** complex problems by separating ingestion, storage, and intelligence logic.
3.  **Detect** patterns (impossible acceleration) that indicate data errors or security threats — and persist the kinematic evidence, not just a flag.
4.  **Visualize** actionable intelligence, filtering noise to focus user attention on anomalies and *why* they fired.

## 🏗 System Architecture
The system follows a microservices architecture, fully containerized with Docker:

* **Ingestor Service (Python):** Polls OpenSky `/states/all` for the configured bounding box (default: Switzerland). Handles rate limiting with exponential backoff and `Retry-After`, supports optional OAuth2 client credentials, filters out on-ground traffic and null coordinates, batch-inserts idempotently (`execute_values` + `ON CONFLICT DO NOTHING`), and prunes history past the retention window.
* **Persistence Layer (PostgreSQL + PostGIS):** Positions stored as SRID-4326 geometry with a composite `(icao24, last_contact)` unique index backing every window query. Schema is managed by idempotent migrations that each service applies at startup under an advisory lock — no manual migration step, no drift between fresh and long-running databases.
* **Intelligence/Detector (Python):** A decoupled worker that compares consecutive observations per aircraft inside a sliding window. A velocity change above the threshold — between observations close enough in time to be comparable — is persisted to `anomaly_events` with the full evidence: speed before/after, delta, time gap, and implied acceleration in m/s². Detection stays isolated from ingestion, so a slow query never blocks data capture.
* **API & Frontend (FastAPI + Leaflet):** The API serves the map itself at `/` and a GeoJSON feed at `/flights` that joins latest positions with recent anomaly evidence. The UI prioritizes **alert hierarchy** — normal traffic is blue/static, anomalies are red/pulsing — and every anomaly popup explains the detection in one line, e.g. *"Speed jumped 63.7 m/s in 10s (6.4 m/s² implied), exceeding the 30 m/s threshold"*. The newest anomaly's popup opens automatically.

## 🚀 How to Run
Prerequisites: Docker & Docker Compose.

1.  **Clone the repo:**
    ```bash
    git clone https://github.com/whs224/geospatial-anomaly-detector.git
    cd geospatial-anomaly-detector
    ```
2.  **(Optional) Configure:**
    ```bash
    cp .env.example .env
    ```
    Everything runs with defaults, but OpenSky's anonymous tier only grants ~400 API credits/day (the default Switzerland bounding box costs 1 credit per request). A free OpenSky account gets 4,000/day — create an API client under *Account → API Client* and put the credentials in `.env`.
3.  **Start the pipeline:**
    ```bash
    docker compose up --build -d
    ```
4.  **Open the Intelligence Map:**
    `http://localhost:8000` — service health lives at `http://localhost:8000/health`.

### Configuration
All tunables are environment variables (see `.env.example` and `config.py`): bounding box, fetch interval, velocity threshold, max comparable time gap, active-flight window, anomaly display TTL, and retention hours.

### Tests
```bash
pip install -r requirements-dev.txt
pytest
```

## 🧠 Technical Decisions & Trade-offs
* **PostGIS from day one:** Positions are stored as indexed geometry rather than bare floats, so spatial predicates (corridor filters, sector polygons) become database queries instead of application-layer math as the feature set grows.
* **Decoupled architecture:** By separating the *Detector* from the *Ingestor*, the system stays resilient — if analysis hangs on a complex query, ingestion continues uninterrupted.
* **Evidence over flags:** The detector persists *why* an anomaly fired (delta-v, time gap, implied acceleration), so the API reads detection results instead of re-deriving them — one source of truth, and alerts survive restarts.
* **Idempotency everywhere:** OpenSky re-serves a state vector when an aircraft hasn't transmitted between polls; the `(icao24, last_contact)` unique key makes re-ingestion and re-detection no-ops rather than data corruption.
* **Physics-aware thresholds:** A raw velocity delta means nothing without the time span it happened over. Pairs further apart than the max-gap window are never compared (an aircraft leaving and re-entering coverage isn't an anomaly), and the implied acceleration is recorded with every event.
* **Bounded queries on unbounded streams:** Every hot query is windowed to recent data and backed by the composite index, and retention pruning keeps the table from growing without limit — the system's cost profile is flat over time.

## 📸 Screenshot
![Dashboard Screenshot](demo.png)
