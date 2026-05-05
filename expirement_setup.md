# CorrectExam — Local Setup Guide

Everything you need to start the full stack locally: MySQL, Minio, backend (Quarkus), and frontend (Angular).

---

## Prerequisites

| Tool | Required version | Check |
|---|---|---|
| Java | 17, 19, 20, or 21 (NOT 22/23) | `java -version` |
| Maven wrapper | bundled (`./mvnw`) | — |
| Node.js | ≥ 22.9.0 | `node --version` |
| npm | ≥ 10 | `npm --version` |
| Docker | any recent version | `docker info` |

> **Java 23 workaround:** The Maven enforcer only allows Java 17–21. If you have Java 23 (e.g. installed via Homebrew), append `-Denforcer.skip=true` to every `./mvnw` command. See Step 4.

---

## Step 1 — Start MySQL (Docker)

The dev profile expects MySQL on `localhost:3306`, database `gradeScopeIstic`, user `gradescope`, password `test`.

```bash
docker run -d --name correctexam-mysql \
  -e MYSQL_DATABASE=gradeScopeIstic \
  -e MYSQL_USER=gradescope \
  -e MYSQL_PASSWORD=test \
  -e MYSQL_ROOT_PASSWORD=rootpassword \
  -p 3306:3306 \
  mysql:8.0 --lower_case_table_names=1 --skip-ssl --character_set_server=utf8mb4
```

Wait until MySQL is ready to accept connections:

```bash
until docker exec correctexam-mysql mysqladmin ping -u root -prootpassword --silent 2>/dev/null; do
  echo "Waiting for MySQL..."; sleep 3
done
echo "MySQL is ready!"
```

On subsequent runs, if the container already exists, just start it:

```bash
docker start correctexam-mysql
```

---

## Step 2 — Start Minio (Docker)

Minio is the S3-compatible object store used to save uploaded PDF scans.

```bash
docker run -d --name correctexam-minio \
  -p 9000:9000 -p 9090:9090 \
  -e "MINIO_ROOT_USER=admin" \
  -e "MINIO_ROOT_PASSWORD=minioadmin" \
  quay.io/minio/minio server /data --console-address ":9090"
```

On subsequent runs:

```bash
docker start correctexam-minio
```

Minio web console is available at http://localhost:9090 (login: `admin` / `minioadmin`).

---

## Step 3 — Fix the JDBC URL (one-time, already applied)

MySQL 8.0 uses the `caching_sha2_password` auth plugin by default, which requires adding `allowPublicKeyRetrieval=true` to the connection string. This was already patched in `corrigeExamBack/src/main/resources/application.properties`:

```properties
# Before (original)
%dev.quarkus.datasource.jdbc.url=jdbc:mysql://localhost:3306/gradeScopeIstic?useUnicode=true&characterEncoding=utf8&useSSL=false

# After (fixed)
%dev.quarkus.datasource.jdbc.url=jdbc:mysql://localhost:3306/gradeScopeIstic?useUnicode=true&characterEncoding=utf8&useSSL=false&allowPublicKeyRetrieval=true
```

Nothing to do here on subsequent runs — the fix is already committed in the file.

---

## Step 4 — Start the Backend (Quarkus)

```bash
cd corrigeExamBack
```

### First run only — seed the database

The first time you start the backend you must run Liquibase migrations to create all tables and insert seed data (two default users: `user:user` and `admin:admin`):

```bash
./mvnw quarkus:dev -Dquarkus.liquibase.migrate-at-start=true -Denforcer.skip=true
```

Wait until you see:

```
correctexam 1.0.0-SNAPSHOT on JVM (powered by Quarkus 3.25.4) started in X.XXXs. Listening on: http://localhost:8082
```

### Subsequent runs

```bash
./mvnw quarkus:dev -Denforcer.skip=true
```

> `-Denforcer.skip=true` is only needed if your Java version is outside 17–21.  
> Remove it if you have Java 17 or 21 installed.

The backend runs on **http://localhost:8082** with live reload enabled.  
Swagger UI is at **http://localhost:8082/swagger-ui**.

#### Schema validation warnings (non-fatal)

You may see Hibernate warnings like:

```
Schema-validation: wrong column type encountered in column [json_data] in table [comments];
found [longtext (Types#LONGVARCHAR)], but expecting [tinytext (Types#CLOB)]
```

These are safe to ignore in dev — the app starts and works normally. To fix them permanently, run the SQL printed in the log against your database.

---

## Step 5 — Install Frontend Dependencies (one-time)

```bash
cd corrigeExamFront
npm install --legacy-peer-deps
```

`--legacy-peer-deps` is required because some transitive dependencies have peer conflicts that don't affect runtime behaviour.

---

## Step 6 — Start the Frontend (Angular)

Make sure the backend is already running on `:8082` before starting the frontend, as it proxies API calls there.

```bash
cd corrigeExamFront
npm start
```

This is equivalent to:

```bash
FRONT_URL=/ SERVER_API_URL=/ node --max-old-space-size=4096 ./node_modules/@angular/cli/bin/ng serve
```

The dev server runs on **http://localhost:8080**.  
It proxies `/api`, `/management`, `/v3/api-docs`, and `/swagger-ui` requests to `:8082` automatically (configured in `webpack/proxy.conf.js`).

---

## Default Login Credentials

| Username | Password | Role |
|---|---|---|
| `user` | `user` | Regular user |
| `admin` | `admin` | Administrator |

---

## Quick Reference — All Services

| Service | URL | Docker container |
|---|---|---|
| Frontend | http://localhost:8080 | — |
| Backend API | http://localhost:8082/api | — |
| Swagger UI | http://localhost:8082/swagger-ui | — |
| MySQL | localhost:3306 | `correctexam-mysql` |
| Minio console | http://localhost:9090 | `correctexam-minio` |
| Minio API | http://localhost:9000 | `correctexam-minio` |

---

## Stopping Everything

```bash
# Stop Docker containers (data is preserved)
docker stop correctexam-mysql correctexam-minio

# Stop backend: Ctrl+C in its terminal

# Stop frontend: Ctrl+C in its terminal
```

To remove the containers entirely (loses all data):

```bash
docker rm -f correctexam-mysql correctexam-minio
```

---

## Full Startup Order (summary)

```
1. docker start correctexam-mysql
2. docker start correctexam-minio
3. cd corrigeExamBack && ./mvnw quarkus:dev -Denforcer.skip=true   # wait for "started"
4. cd corrigeExamFront && npm start                                  # wait for "Compiled successfully"
5. Open http://localhost:8080
```

---

---

# Docker Compose Setup (Full Stack + Monitoring)

An alternative to the manual setup above. Everything — MySQL, Minio, backend, frontend, Prometheus, Grafana, and exporters — runs as Docker containers started with a single command. No local Java or Node.js required.

## Prerequisites

| Tool | Notes |
|---|---|
| Docker Desktop | Any recent version with Compose v2 |
| Docker Hub account | Required to push/pull images |

## Repository Structure (added files)

```
CorrigeExam/
├── docker-compose.yml                  ← Full stack definition
├── .env                                ← DOCKERHUB_USERNAME and IMAGE_TAG
├── build-push.sh                       ← Build images and publish to Docker Hub
├── corrigeExamBack/
│   ├── Dockerfile                      ← Multi-stage: Maven build → JRE 17 runtime
│   └── .dockerignore
├── corrigeExamFront/
│   ├── Dockerfile                      ← Multi-stage: Node 22 build → nginx
│   ├── nginx.conf                      ← Proxies /api, /management, etc. → back:8080
│   └── .dockerignore
└── monitoring/
    ├── prometheus/prometheus.yml       ← Scrapes backend, node-exporter, cadvisor
    └── grafana/provisioning/
        ├── datasources/datasource.yml
        └── dashboards/
            ├── dashboard.yml
            └── JVM.json               ← Pre-built JVM Micrometer dashboard
```

## Step 1 — Configure Docker Hub username

Edit `.env` at the repo root:

```
DOCKERHUB_USERNAME=your-dockerhub-username
IMAGE_TAG=latest
```

## Step 2 — Build and publish images to Docker Hub

Run once from the repo root (takes ~15 min on first build — Maven and npm download dependencies):

```bash
docker login          # authenticate to Docker Hub
./build-push.sh       # builds backend + frontend images and pushes them
```

Images published:
- `<DOCKERHUB_USERNAME>/correctexam-back:latest`
- `<DOCKERHUB_USERNAME>/correctexam-front:latest`

> **Note:** The backend Dockerfile copies `pom.xml`, `sonar-project.properties`, `.mvn/`, and `src/`. The `sonar-project.properties` file must be present at the root of `corrigeExamBack/` — it is read by the Maven properties plugin during the build.

## Step 3 — Start the full stack

```bash
docker compose up -d
```

Docker Compose starts services in dependency order:

```
mysql ──────────────────────────────────┐
minio ─────────────────────────────────→ back → front
minio-init (creates bucket, then exits)─┘

prometheus, grafana, node-exporter, cadvisor  (start in parallel)
```

Wait ~30 seconds after `docker compose up -d` for the backend to finish Liquibase migrations before using the app.

## Service URLs

| Service | URL | Credentials |
|---|---|---|
| Frontend | http://localhost:8080 | user/user or admin/admin |
| Backend API | http://localhost:8082/api | — |
| Swagger UI | http://localhost:8082/swagger-ui | — |
| Minio console | http://localhost:9090 | admin / minioadmin |
| Prometheus | http://localhost:9092 | — |
| Grafana | http://localhost:3000 | admin / admin |

## Useful commands

```bash
# Check all service statuses
docker compose ps

# Follow backend logs (including Liquibase migration output)
docker logs -f correctexam-back

# Follow any service logs
docker logs -f correctexam-front

# Stop everything (data volumes are preserved)
docker compose down

# Stop and delete all data volumes (full reset)
docker compose down -v

# Restart a single service after a config change
docker compose restart back

# Pull latest images from Docker Hub (on a remote server)
docker compose pull && docker compose up -d
```

## On a remote server (no source code required)

Copy only these files to the server:

```
docker-compose.yml
.env
monitoring/
```

Then:

```bash
docker compose pull   # pulls images from Docker Hub
docker compose up -d
```

## Troubleshooting

**Backend fails to start (DB connection refused):** MySQL healthcheck needs to pass first. Wait 30 s and check `docker logs correctexam-mysql`. If the `gradeScopeIstic` database or `gradescope` user is missing, the volume may have been created with a different config — run `docker compose down -v` and restart.

**Minio bucket missing:** The `minio-init` one-shot container creates the `test` bucket on first start. If it exited with an error, check `docker logs correctexam-minio-init` and re-run `docker compose restart minio-init`.

**Port conflicts:** If ports 8080, 8082, 3306, 9000, 9090, 9092, or 3000 are in use by other processes (e.g. the manual dev stack), stop those first before running `docker compose up -d`.
