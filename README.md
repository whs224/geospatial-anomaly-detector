# Real-Time Geospatial Anomaly Detector

A distributed intelligence pipeline that ingests live flight telemetry, detects kinematic anomalies in real-time, and visualizes high-priority targets for operator review.

## 🎯 Project Goal
To move beyond static data analysis and build a **living system** that can:
1.  **Ingest** messy, high-velocity data streams (OpenSky Network API).
2.  **Decompose** complex problems by separating ingestion, storage, and intelligence logic.
3.  **Detect** patterns (e.g., impossible acceleration) that indicate data errors or security threats.
4.  **Visualize** actionable intelligence, filtering noise to focus user attention on anomalies.

## 🏗 System Architecture
The system follows a microservices architecture, fully containerized with Docker:

* **Ingestor Service (Python):** Connects to external APIs, handles rate-limiting/failures, cleans dirty data (null island filtering), and streams standardized data to the persistence layer.
* **Persistence Layer (PostgreSQL + PostGIS):** Uses spatial indexing (R-Tree) to handle geometric queries efficiently, avoiding the latency of application-layer math.
* **Intelligence/Detector (Python):** A decoupled worker process that scans the windowed state of all objects. It applies kinematic logic (delta velocity / time) to flag anomalies without blocking the ingestion pipeline.
* **API & Frontend (FastAPI + Leaflet):** A user-centric interface that prioritizes **alert hierarchy**—normal traffic is blue/static, while anomalies are red/pulsing to minimize time-to-insight.

## 🚀 How to Run
Prerequisites: Docker & Docker Compose.

1.  **Clone the repo:**
    ```bash
    git clone [https://github.com/yourusername/geo-project.git](https://github.com/yourusername/geo-project.git)
    cd geo-project
    ```
2.  **Start the pipeline:**
    ```bash
    docker-compose up --build
    ```
3.  **Access the Intelligence Map:**
    Open `http://localhost:8000` in your browser.

## 🧠 Technical Decisions & Trade-offs
* **PostGIS vs. Python Math:** I chose PostGIS because spatial operations (like "find flights within this polygon") scale O(log n) with database indexing, whereas Python lists would be O(n).
* **Decoupled Architecture:** By separating the *Detector* from the *Ingestor*, the system remains resilient. If the analysis logic hangs on a complex query, the data ingestion continues uninterrupted.

## 📸 Screenshot
![Dashboard Screenshot](link-to-your-screenshot.png)