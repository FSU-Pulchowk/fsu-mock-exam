# IOE Entrance Mock Examination Portal - FSU Pulchowk Campus

A high-concurrency Computer-Based Test (CBT) mock exam portal designed to emulate the Tribhuvan University Institute of Engineering (IOE) entrance examination environment.

## 🚀 Key Features & Performance Architecture

- **High-Concurrency Engine**: Migrated to **FastAPI + Uvicorn/Gunicorn (ASGI)** to handle peak concurrent student loads (1K–20K connections).
- **In-Memory Question Cache**: Loads question sets (`data\sets_i.json`) and answer keys directly into RAM at startup for zero disk I/O latency and microsecond API response times.
- **Server-Side Scoring**: Computes scores securely with negative marking (-0.1 per wrong answer in Sec A, -0.2 in Sec B).
- **Exam Time-Window Gating**: Environment-driven exam scheduling (`EXAM_START` / `EXAM_END`) to lock and unlock exam access automatically.
- **DDoS & Flood Protection**: IP-based rate limiting via `slowapi` (`120 req/min`).
- **Nginx Reverse Proxy Ready**: Pre-configured for hosting under subpath `/mock-exam/` on `https://fsu.pcampus.edu.np` (Port `6090`).

## 🛠️ Technology Stack

- **Backend**: Python 3.12, FastAPI, Uvicorn, Gunicorn, Pydantic v2, SlowAPI
- **Frontend**: HTML5, Vanilla CSS3, JavaScript (Fetch API)
- **Deployment**: Nginx Reverse Proxy (SSL/TLS), Systemd Service on Ubuntu
