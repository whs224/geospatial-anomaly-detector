# Real-Time Geospatial Anomaly Detector

A distributed pipeline that ingests live flight telemetry over the US Northeast corridor (the Boston–New York–Washington airspace), flags aircraft whose implied acceleration (ground-speed change over the time gap) exceeds a configurable threshold, classifies each aircraft into its airspace sector with a PostGIS spatial join, and visualizes those outliers — with the kinematic evidence behind every flag — on a live map.

This README is written to double as a technical reference: every feature described below is confirmed against the source as it currently exists, and a **Known Limitations** section states plainly where things are approximations or gaps.

## 🎯 Project Goal
A working, containerized system that:
1. **Ingests** a high-velocity public data stream (the OpenSky Network API) with rate-limit-aware backoff and idempotent writes.
2. **Separates** ingestion, storage, and detection into independent services that share only a database.
3. **Detects** kinematic outliers — aircraft whose implied acceleration exceeds a configurable threshold — as a first-pass filter for unusual motion or data-quality issues, and persists the kinematic evidence, not just a flag. The threshold is currently set toward sensitivity (it surfaces events readily), so routine departures and climb-outs trip it too.
4. **Classifies** each aircraft into an airspace sector with a real, GiST-indexed spatial query, and visualizes the result.

## 🏗 System Architecture
Four containers, defined in `docker-compose.yml`. **They communicate only through the shared PostgreSQL database — there are no direct service-to-service calls (no HTTP/RPC between them).** The only coupling is the schema.

| Service | Container | Code | Role |
|---|---|---|---|
| **db** | `geo-postgres` | `postgis/postgis:15-3.3` (pulled image) | PostgreSQL + PostGIS. Stores positions, anomaly events, and sector polygons. |
| **ingestor** | `geo-ingestor` | `main.py` | Polls OpenSky every 10s, filters, batch-inserts positions, prunes old rows. |
| **detector** | `geo-detector` | `detector.py` | asyncio loop; every 10s compares consecutive observations and writes anomalies. |
| **api** | `geo-api` | `api.py` (FastAPI/uvicorn) | Serves the map at `/`, the flight feed at `/flights`, sectors at `/sectors`, health at `/health`. |

The three application services (`ingestor`, `detector`, `api`) build from the same `Dockerfile` (`python:3.11-slim-bookworm`, non-root `app` user). `db` is the upstream PostGIS image.

### Data flow (end to end)
```
OpenSky /states/all
      │  (ingestor: HTTP GET every 10s, OAuth2 bearer if credentials set)
      ▼
parse_flight_state → filter out on-ground / null-coordinate / keyless vectors
      │  (batch INSERT ... ON CONFLICT DO NOTHING, geometry = ST_SetSRID(ST_MakePoint(lon,lat),4326))
      ▼
flight_positions  ── GEOMETRY(Point, 4326), unique (icao24, last_contact)
      │                                   │
      │ (detector reads recent rows)      │ (api reads latest positions)
      ▼                                   ▼
CANDIDATE_QUERY (LAG window,        LATEST_POSITIONS_QUERY  +  RECENT_ANOMALIES_QUERY
 max-gap, accel threshold)           +  LEFT JOIN LATERAL ST_Contains(sector)
      │                                   │
      ▼                                   ▼
anomaly_events  ──────────────────►  /flights GeoJSON  (position + is_anomaly + evidence + sector)
 (unique (icao24, last_contact))          │
                                          ▼
                                   Leaflet map (index.html): markers, sector overlays, popups
```
All three application services apply the idempotent migrations at startup under a shared advisory lock — the ingestor and API via `db.apply_migrations()`, the async detector via its own `_apply_migrations()` (which reuses the same lock id and SQL) — so any of them can bring a fresh database up to schema on its own.

## 🚀 How to Run
Prerequisites: Docker & Docker Compose.

1. **Clone and (optionally) configure:**
   ```bash
   git clone https://github.com/whs224/geospatial-anomaly-detector.git
   cd geospatial-anomaly-detector
   cp .env.example .env   # optional; everything has working defaults
   ```
   OpenSky's anonymous tier grants ~400 API credits/day; the default US Northeast box costs **2 credits per request** (see *Data ingestion* below for the math). A free registered account gets ~4,000/day — create an API client under *Account → API Client* and put the credentials in `.env`.
2. **Start the pipeline:**
   ```bash
   docker compose up --build -d
   ```
3. **Open the map:** `http://localhost:8000` — health at `http://localhost:8000/health`.

Both `8000` (API) and `5432` (Postgres) are bound to `127.0.0.1` only, so nothing is exposed off the host.

## 🛰 Data ingestion and rate limiting
`main.py` runs a synchronous loop (`opensky.py` is the HTTP client):

- **Polling.** Every `FETCH_INTERVAL_SECONDS` (default **10s**) it GETs `/states/all` with the bounding box params `lamin/lomin/lamax/lomax`. The current box is **`38.0, -78.0` → `43.0, -70.0`** (lat 38–43°N, lon 78–70°W) — the US Northeast corridor from Washington/Baltimore up through Philadelphia, New York, and Boston (~40 square degrees).
- **Authentication.** OAuth2 client-credentials (`opensky.py._refresh_token`): if `OPENSKY_CLIENT_ID`/`OPENSKY_CLIENT_SECRET` are set it fetches a bearer token, caches it, and refreshes 60s before expiry; otherwise it polls anonymously.
- **Credit math.** OpenSky charges API credits by requested area: ~40 sq deg falls in the 25–100 tier = **2 credits/request**. At 10s that is 360 requests/hour = **720 credits/hour**. So a full **authenticated** day (~4,000 credits) lasts ~5.5 hours of continuous polling before throttling; **anonymous** (~400) lasts ~33 minutes. This is a light-usage budget, not a 24/7 one.
- **Filtering** (`parse_flight_state`): drops vectors with no `icao24`, no `last_contact`, null latitude/longitude, or `on_ground = true`. `last_contact` is converted to a timezone-aware UTC datetime.
- **Idempotent batch insert** (`insert_positions`): `psycopg2.extras.execute_values` inserts up to 500 rows/page with `ON CONFLICT (icao24, last_contact) DO NOTHING`, building the geometry with `ST_SetSRID(ST_MakePoint(lon, lat), 4326)`. OpenSky re-serves a vector when an aircraft hasn't transmitted since the last poll, so the unique key makes re-ingestion a no-op.
- **Retention pruning** (`prune_old_rows`): every `PRUNE_INTERVAL_SECONDS` (default 3600s) it deletes `flight_positions` and `anomaly_events` older than `RETENTION_HOURS` (default 24h).
- **Rate-limit / error handling.** A `429` raises `RateLimitedError` carrying `Retry-After`; the loop waits `max(Retry-After, backoff)` where `backoff` doubles each failure up to `BACKOFF_MAX_SECONDS` (900s). Other OpenSky errors and database errors back off the same way (the DB error path also drops and reconnects).

## 🗄 Persistence, idempotency, and migrations
Schema lives in `migrations.sql`; `db.apply_migrations()` runs it at startup.

- **Tables.** `flight_positions` (id, icao24, callsign, velocity, heading, `last_contact TIMESTAMPTZ`, `geom GEOMETRY(Point,4326)`); `anomaly_events` (the full evidence: prev/new velocity, delta_v, time_gap_seconds, implied_accel, threshold, prev_contact, last_contact, detected_at); `sectors` (code, name, `geom GEOMETRY(Polygon,4326)`).
- **Idempotency.** Both `flight_positions` and `anomaly_events` carry a **`UNIQUE (icao24, last_contact)`** constraint, and every writer uses `ON CONFLICT (icao24, last_contact) DO NOTHING`. This makes re-ingestion and re-detection no-ops instead of duplicates — the same observation or anomaly can be seen many times across overlapping windows and only lands once.
- **Advisory-lock migrations.** `db.apply_migrations()` opens a connection, takes **`pg_advisory_xact_lock(74123)`** (a transaction-scoped lock all services share), then executes `migrations.sql` and commits (releasing the lock). All three services apply the migrations at startup under the same lock id — the ingestor and API through `db.apply_migrations()`, the async detector through its own `_apply_migrations()` that reuses `db._MIGRATION_LOCK_ID` and `migrations.sql` over the async driver — so concurrent boots **serialize**: no two run the DDL at once. The SQL is written to be re-runnable (`CREATE TABLE/INDEX IF NOT EXISTS`, guarded `DO $$` blocks, `INSERT ... ON CONFLICT DO NOTHING`), so fresh and long-running databases converge on the same schema with no manual migration step.
- **Indexes.** `uq_flight_positions_icao24_last_contact` (unique composite, also serves the window queries), `idx_flight_positions_last_contact`, `idx_flight_positions_geom` (GiST), `idx_anomaly_events_detected_at`, `idx_sectors_geom` (GiST).

## 🗺 The spatial query feature (end to end)
This is a genuine PostGIS spatial predicate, not stored geometry that goes unused.

**1. The sectors.** `migrations.sql` seeds a `sectors` table with **four** polygons, each an **octagon — 8 distinct vertices plus the closing point** (confirmed in the WKT). They are **approximations of the real TRACON (Terminal Radar Approach Control) airspaces**, positioned over the airport clusters in the box:

| Code | Name | Approx. center |
|---|---|---|
| `N90` | New York TRACON | 40.75°N, 73.9°W |
| `A90` | Boston TRACON | 42.37°N, 71.0°W |
| `PHL` | Philadelphia TRACON | 39.87°N, 75.24°W |
| `PCT` | Potomac TRACON (DC/Baltimore) | 38.95°N, 77.0°W |

Example (N90), verbatim from `migrations.sql`:
```
POLYGON((-73.18 40.75, -73.3909 41.1389, -73.9 41.3, -74.4091 41.1389,
         -74.62 40.75, -74.4091 40.3611, -73.9 40.2, -73.3909 40.3611,
         -73.18 40.75))
```
Each is stored as `GEOMETRY(Polygon, 4326)` with a **GiST index** (`idx_sectors_geom`).

**2. The predicate.** `api.py`'s `LATEST_POSITIONS_QUERY` takes the latest position per active aircraft, then `LEFT JOIN LATERAL` against `sectors` on **`ST_Contains(sectors.geom, aircraft.geom)`** — a true point-in-polygon test — keeping one sector (`ORDER BY s.id LIMIT 1`). `LEFT JOIN` so an aircraft in no sector comes back with `NULL` (rendered as "En route"). The result is exposed as `sector`/`sector_code` in each `/flights` feature; a separate `/sectors` endpoint returns the polygons themselves (via `ST_AsGeoJSON`) for the map overlay.

**3. Why GiST, and the two-phase behavior.** `ST_Contains` is evaluated in two stages, and the GiST index accelerates the first:
- **Stage 1 (index-served):** the GiST tree stores each polygon's bounding box; it quickly finds sectors whose bbox could contain the point (the `geom ~ point` operator). This discards sectors that obviously can't match.
- **Stage 2 (exact):** the precise point-in-polygon geometry runs only on that small candidate set.

**4. Confirmed from `EXPLAIN ANALYZE`** — the planner *does* use the index:
- The `/flights` join plan shows **`Index Scan using idx_sectors_geom on sectors`**, `Index Cond: (geom ~ flight_positions.geom)`, then `Filter: st_contains(...)` (~6 ms execution).
- Classifying all ~68k stored rows against one sector shows **`Bitmap Index Scan on idx_flight_positions_geom`**, `Index Cond: (geom @ $0)` (~31 ms).

**Real vs. approximated (be explicit):** *Real* — a genuine spatial predicate (`ST_Contains`), on a GiST-indexed geometry column, that the planner actually serves from the index; the point-in-polygon runs in the database, not in Python. *Approximated* — the polygon shapes are octagonal stand-ins, not exact FAA TRACON boundaries; they are positioned over the real airports at a plausible size but should be described as approximations.

## 🧠 The detector and anomaly logic
`detector.py` runs `CANDIDATE_QUERY` every `DETECTION_INTERVAL_SECONDS` (default **10s**):

- **Windowed comparison.** A CTE selects positions from the last `DETECTION_LOOKBACK_SECONDS` (default 85s). A SQL **window function, `LAG(velocity)`/`LAG(last_contact)` `OVER (PARTITION BY icao24 ORDER BY last_contact)`**, pairs each observation with the previous one *for the same aircraft*.
- **Threshold on implied acceleration.** A pair is anomalous when `ABS(new_velocity - prev_velocity) / EXTRACT(EPOCH FROM (last_contact - prev_contact))` exceeds **`ACCEL_THRESHOLD_MS2` (default 2.0 m/s²)**. This is *implied acceleration* — the rate of change of OpenSky's reported **ground speed**, not true airspeed, so it also reflects turns and wind shifts. Thresholding on acceleration (rather than a raw velocity delta) means a gradual enroute change no longer fires; only an abrupt one does.
- **Max-time-gap cap.** Pairs more than `MAX_TIME_GAP_SECONDS` apart (default `2.5 × FETCH = 25s`) are never compared, so an aircraft that left the box and returned later can't produce a false pair. A `last_contact > prev_contact` guard keeps the division safe.
- **Evidence, not a flag.** `anomaly.py::build_anomaly_records` computes the full evidence (speed before/after, delta_v, time_gap, implied_accel) and `persist_anomalies` writes it to `anomaly_events`. The API later *reads* this evidence rather than recomputing it — one source of truth, and alerts survive restarts. The popup one-liner (`format_summary`) reads e.g. *"Implied acceleration 3.4 m/s² over 8s exceeds the 2.0 m/s² threshold."*

## ⚙️ The concurrency model
Documented as it actually is in the code:

- **detector — genuine asyncio.** `detector.py` runs on an **asyncio event loop** (`asyncio.run(_amain())`) using the **async psycopg (psycopg3)** driver. The cycle `await`s the candidate query and the anomaly writes (`await cursor.execute/fetchall/fetchone`, `await conn.commit()`), the inter-cycle wait is an awaited, interruptible sleep rather than `time.sleep`, and SIGTERM is handled with `loop.add_signal_handler` so `docker stop` drains the current cycle and exits cleanly. Startup (`wait_for_db`, advisory-lock migrations) also runs over the async driver.
  - **Honest caveat:** with a *single* detection query per cycle and no other concurrent work, async here is about **structure and non-blocking I/O, not raw speed**. Awaiting one query doesn't make it finish faster, and nothing else runs in the gap. "Asynchronous" is now technically accurate (real event loop, genuinely awaited I/O); the throughput benefit of async would only appear with concurrent awaited work, which this loop doesn't have.
- **ingestor — synchronous worker.** `main.py` is a plain `while True` loop with blocking `psycopg2` calls and a blocking heartbeat-sleep. It is **not** asynchronous — it's a decoupled worker in its own process/container. Its isolation from the detector comes from being a separate container, not from async.
- **api — sync handlers on an async server.** FastAPI runs on the uvicorn ASGI server, but the route handlers are defined with `def` (not `async def`) and use the synchronous `psycopg2` connection pool (`db.create_pool`, min 1 / max 5). FastAPI therefore runs each handler in a threadpool. So request concurrency comes from the threadpool + pooled connections, **not** from async DB calls.
- **db** is the PostGIS server process.

Because the services share nothing but the database, "if the detector's query hangs, ingestion keeps running" is literally true — they are different processes.

## 🩺 Health, lifecycle, and containers
- **Loop healthchecks.** `ingestor` and `detector` touch `/tmp/heartbeat` every cycle; the healthcheck (`find /tmp/heartbeat -newermt '-190 seconds'`) marks the container unhealthy if the file goes stale. **Note:** 190s was sized for a 60s-era cadence; at 10s it is very lenient (a hang is caught in ~190s rather than ~3 cycles). It never false-trips but is slower to notice a hang.
- **API healthcheck.** Hits `/health`, which runs `SELECT 1` through the pool.
- **Graceful shutdown.** Ingestor uses a SIGTERM→`SystemExit` handler; the detector uses asyncio signal handling. Both exit cleanly on `docker stop`.
- **Hardening.** Non-root `app` user, pinned dependencies, and a `.dockerignore` that keeps `.env`, `.git`, `tests/`, and the README out of the image. Ports bound to loopback.

## 🔧 Configuration reference
All tunables are environment variables read in `config.py` (defaults shown); `docker-compose.yml` passes them through, and unset/blank values fall back to the `config.py` defaults.

| Variable | Default | Meaning |
|---|---|---|
| `BBOX_LAMIN/LOMIN/LAMAX/LOMAX` | `38.0 / -78.0 / 43.0 / -70.0` | Ingestion bounding box (US Northeast corridor). |
| `FETCH_INTERVAL_SECONDS` | `10` | Ingestor poll cadence. |
| `ACCEL_THRESHOLD_MS2` | `2.0` | Implied-acceleration threshold for an anomaly. |
| `DETECTION_INTERVAL_SECONDS` | `10` | Detector cycle cadence. |
| `MAX_TIME_GAP_SECONDS` | `2.5 × FETCH` = `25` | Max gap between comparable observations. |
| `DETECTION_LOOKBACK_SECONDS` | `MAX_GAP + 60` = `85` | How far back each detection cycle looks. |
| `ACTIVE_WINDOW_SECONDS` | `3 × FETCH` = `30` | A flight is "active" (on the map) if heard within this. |
| `ANOMALY_TTL_SECONDS` | `300` | How long an anomaly keeps a flight highlighted. |
| `RETENTION_HOURS` / `PRUNE_INTERVAL_SECONDS` | `24` / `3600` | Retention window and prune cadence. |
| `BACKOFF_MAX_SECONDS` | `900` | Cap on exponential backoff. |
| `OPENSKY_CLIENT_ID` / `_SECRET` | empty | Optional OAuth2 credentials. |

## ✅ Tests
```bash
pip install -r requirements-dev.txt
pytest
```
`tests/test_parsing.py` (10 cases) covers `parse_flight_state`; `tests/test_anomaly.py` (7 cases) covers `build_anomaly_records` and `format_summary`. These are pure-function unit tests (no database). See the limitations below for what is **not** covered.

## ⚠️ Known limitations and honest caveats
Things a sharp reviewer would probe — stated plainly:

- **Sector polygons are approximate, not exact.** They are octagonal stand-ins over the real airports, not real FAA TRACON boundaries. The spatial predicate and GiST index are real; the shapes are approximations.
- **"Implied acceleration" is derived from ground speed, not airspeed.** OpenSky's `velocity` is horizontal ground speed, so turns and wind shifts register as "acceleration." It's labeled *implied* for that reason; it's a first-pass kinematic filter, not a certified anomaly classifier.
- **The 2.0 m/s² threshold is set for sensitivity, not specificity.** A departing airliner routinely pulls ~1.5–2.5 m/s², so at this setting the detector flags normal takeoffs/climb-outs, not just genuine anomalies. Raising the threshold isolates rarer, more extreme events.
- **Anomaly count depends on airspace phase, not flight count.** Anomalies cluster near airports (departures/climbs); a wide box full of cruising aircraft produces few, because cruise acceleration is ~0. More flights ≠ more anomalies.
- **No horizontal scaling.** Four single-instance containers; there are no replicas or `deploy` directives. Docker Compose provides orchestration, not scaling. Multiple ingestors would double-poll; the API is the only trivially replicable service.
- **The detection SQL is not covered by an automated test.** The unit tests cover parsing and evidence-building; `CANDIDATE_QUERY` (the LAG window, the max-gap and threshold predicates) is verified manually/live, not by an integration test against a real Postgres.
- **The async model is single-query.** As noted above, the detector's asyncio loop is correct and non-blocking but has no concurrent work to exploit for throughput.
- **PostGIS spatial use is currently one predicate.** The `ST_Contains` sector join is real and GiST-served; beyond it, the geometry is used for storage and coordinate readback (`ST_X`/`ST_Y`). There is no corridor/route spatial analytics yet.
- **The credit budget is limited.** At 2 credits/request and 10s, an authenticated day lasts ~5.5h before throttling; the ingestor then backs off until the daily reset. It is not tuned for continuous 24/7 operation.
- **The `threshold` column is generic.** `anomaly_events.threshold` stores whichever threshold applied at insert time (now the 2.0 m/s² acceleration threshold); rows predating a threshold-semantics change would carry an older value. It self-heals within the retention window.

## 💬 Interview talking points / likely questions
- **"Is the GiST index actually used, or just present?"** Actually used. `EXPLAIN ANALYZE` on the `/flights` join shows `Index Scan using idx_sectors_geom` with `Index Cond: (geom ~ flight_positions.geom)`; at 68k rows the same predicate uses `Bitmap Index Scan on idx_flight_positions_geom`.
- **"Why PostGIS if you're barely doing spatial queries?"** There is one real spatial query — the `ST_Contains` sector classification — and PostGIS is the foundation for more (corridor/sector analytics). Honestly, today it's storing geometry, reading coordinates back, and running that one GiST-served point-in-polygon; I wouldn't claim more.
- **"Walk me through the spatial join."** Positions are `GEOMETRY(Point,4326)`; sectors are `GEOMETRY(Polygon,4326)` with a GiST index. For each aircraft I ask "which sector contains this point?" via `ST_Contains(sector.geom, aircraft.geom)`. GiST does a bbox pre-filter, then the exact point-in-polygon runs on the survivors. A `LEFT JOIN LATERAL … LIMIT 1` gives one sector or NULL (en route).
- **"How does the async detector work — and does it make it faster?"** It's a real asyncio event loop with the async psycopg driver, so the DB calls are awaited and non-blocking. Honestly, with one query per cycle it's not faster than the old synchronous loop — there's no concurrency to overlap. The win is correct non-blocking structure and a foundation for concurrent work, and that "asynchronous" is now accurate.
- **"How do you avoid duplicate data / double-counting anomalies?"** A `UNIQUE (icao24, last_contact)` constraint plus `ON CONFLICT DO NOTHING` everywhere. Overlapping windows re-see the same observation/anomaly; it only lands once.
- **"How do the services coordinate?"** They don't call each other — they share only the database. The ingestor writes positions, the detector reads positions and writes anomalies, the API reads both. A slow query in one can't block another.
- **"How do three services agree on the schema without a migration step?"** Each applies the idempotent `migrations.sql` at startup under a shared Postgres advisory lock, so concurrent boots serialize and there's no drift.
- **"What's the anomaly, physically?"** The rate of change of ground speed over the time between two consecutive reports exceeding a threshold. It flags abrupt kinematic changes — which at the current threshold includes normal departures — not certified security events.

## 📸 Screenshot
![Dashboard Screenshot](demo.png)
