# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Research code for evidence-grounded IaC patch generation. PhD context lives in
`PHD_CONTEXT.md`; potential paper ideas are in `PAPERS.md`. Read those before
making non-trivial changes.

## Project Overview

CorrectExam (GradeScope ISTIC) is an exam scanning, alignment, and grading platform for educators. It is a monorepo with two independent sub-projects:

- `corrigeExamBack/` — Quarkus 3.x (Java 17) REST backend
- `corrigeExamFront/` — Angular 20 frontend

---

## Backend (`corrigeExamBack/`)

### Commands

```bash
# First run — initialise and seed the database
./mvnw quarkus:dev -Dquarkus.liquibase.migrate-at-start=true

# Development mode (hot reload on :8082)
./mvnw

# Run all tests
./mvnw verify

# Production JAR
./mvnw -Pprod clean package

# Native executable (requires GraalVM)
./mvnw package -Pnative
```

### Infrastructure Requirements

The backend requires two services running locally:

**MySQL** (dev/prod profile):
```bash
# Fedora/RHEL
sudo dnf install mariadb-server && sudo systemctl start mariadb
sudo mysql -u root -e "CREATE DATABASE gradeScopeIstic;"
```
Or with Docker: `docker-compose -f src/main/docker/mysql.yml up -d`

**Minio** (S3-compatible file store for PDF uploads):
```bash
podman run -p 9000:9000 -p 9090:9090 -e "MINIO_ROOT_USER=admin" -e "MINIO_ROOT_PASSWORD=minioadmin" \
  quay.io/minio/minio server /data --console-address ":9090"
```

Default credentials (configured in `src/main/resources/application.properties`):
- DB: `gradescope` / `test`, database `gradeScopeIstic`
- Minio: `admin` / `minioadmin`, bucket `test`
- Default seeded users: `user:user` and `admin:admin`

### Quarkus Profiles

| Profile | DB | Notes |
|---|---|---|
| `dev` | MySQL on localhost:3306 | Port 8082, dev tools enabled |
| `prod` | MySQL | Production settings |
| `alone` | H2 file (`./mydb`) | Standalone mode, no Minio (saves to filesystem) |
| `test` | H2 in-memory | Used by `./mvnw verify` |

### Architecture

The backend follows a JHipster Quarkus layered architecture under `src/main/java/fr/istic/`:

- **`domain/`** — JPA entities (Hibernate ORM). Core entities: `Course`, `Exam`, `Template`, `Question`, `Zone`, `Scan`, `ExamSheet`, `Student`, `StudentResponse`, `FinalResult`, `GradedComment`, `TextComment`, `HybridGradedComment`.
- **`service/`** — Business logic services, one per entity. Custom DTOs in `service/customdto/` for non-CRUD operations (PDF export, clustering, predictions, etc.). MapStruct mappers in `service/mapper/`.
- **`web/rest/`** — JAX-RS REST resources. Standard CRUD resources per entity, plus `ExtendedAPI.java` for complex cross-entity operations. `CasAuth.java` and `ShibAuth.java` handle institutional SSO flows.
- **`security/`** — JWT via SmallRye JWT (RSA keys at `src/main/resources/jwt/privateKey.pem`). Roles: `ROLE_USER`, `ROLE_ADMIN`.
- **`config/`** — Quarkus config beans, naming strategies for Hibernate to maintain JHipster-compatible column naming.

PDF processing uses Apache PDFBox. File storage abstraction: `FichierS3Service` handles both Minio (`correctexam.uses3=true`) and local filesystem (`correctexam.saveasfile=true`).

Database schema is managed by Liquibase (`src/main/resources/config/liquibase/master.xml`). Do not modify schema by hand; add Liquibase changesets.

---

## Frontend (`corrigeExamFront/`)

### Commands

```bash
npm install          # Install dependencies (required once, and after package.json changes)

npm start            # Dev server on :4200, proxies /api to :8082
npm test             # Run Jest unit tests (also runs lint first)
npm run lint         # ESLint check
npm run lint:fix     # ESLint auto-fix
npm run prettier:format  # Format all source files

# Run a single test file
npx jest --testPathPattern="component-name" --config jest.conf.js

# Production build
npm run webapp:build:prod

# E2E tests (Cypress)
npm run cy:run
```

The dev server (`npm start`) proxies `/api`, `/management`, `/v3/api-docs`, and other backend paths to `http://127.0.0.1:8082` (configured in `webpack/proxy.conf.js`). Start the backend first.

### Architecture

All application code is under `src/main/webapp/app/`.

The main feature module is **`scanexam/`** — the entire exam workflow lives here:

| Sub-folder | Purpose |
|---|---|
| `mes-cours/`, `coursdetail/`, `creercours/` | Course listing and management |
| `creerexam/`, `creerexamnbgrader/` | Exam creation (standard and nbgrader import) |
| `chargerscan/` | Upload student scan PDFs |
| `viewandreorderpages/` | Page ordering/rotation before alignment |
| `alignscan/` | Scan-to-template alignment (uses OpenCV web worker) |
| `annotate-template/paint/` | Fabric.js canvas for drawing question zones on the template |
| `associer-copies-etudiants/` | Bind exam sheets to student list |
| `corrigequestion/` | Main grading view — annotate student responses, apply comments |
| `voircopie/` | Review a student's full corrected sheet |
| `marking-summary/` | Progress overview across all sheets |
| `exportanonymoupdf/` | Generate annotated PDF per student |
| `statsexam/` | Grade statistics and distributions |
| `mlt/` | ML-based prediction clustering (TensorFlow.js / ONNX Runtime Web) |

**Web Workers** — computationally heavy tasks run off the main thread:
- `opencv.worker.ts` — image alignment using OpenCV.wasm
- `align.pool.worker.ts` — worker pool for parallel alignment
- `dbsqlite.worker.ts` — SQLite worker for offline data

**Browser-side caching** — `scanexam/db/db.ts` defines `ExamIndexDB` (Dexie/IndexedDB wrapper) that caches per-exam scan images (aligned and non-aligned) and template pages in the browser. Each exam gets its own IndexedDB named `correctExam<id>`.

**Routing** — `scanexam/scanexam.route.ts` defines lazy-loaded routes for the main workflow. JHipster-standard entities are in `entities/entity-routing.module.ts`.

**State/Services** — No NgRx; services use RxJS subjects and BehaviorSubjects. HTTP calls go through Angular's `HttpClient` using JHipster-generated service classes (one per entity in the `entities/` folders).

### Key Libraries

- `fabric` — Canvas annotation (drawing zones, free-hand markup)
- `ngx-extended-pdf-viewer` — Render PDFs in browser
- `dexie` — IndexedDB ORM for browser-side image caching
- `@tensorflow/tfjs` + `onnxruntime-web` — In-browser ML predictions
- `primeng` + `@ng-bootstrap/ng-bootstrap` — UI component libraries
- `dayjs` — Date handling

---

## Data Model

Defined in `corrigeExamBack/model.jdl`:

```
Course
 ├── CourseGroup → Student[]
 └── Exam
      ├── Template (PDF template with zones drawn on it)
      ├── Scan (uploaded student answer PDF)
      ├── Question[] → Zone (coordinates on template page)
      │    ├── GradedComment[] (reusable scored annotations)
      │    ├── TextComment[]   (reusable text-only annotations)
      │    └── HybridGradedComment[]
      └── ExamSheet[] ↔ Student[]
           └── StudentResponse (per student per question)
                ├── FinalResult
                └── Comments (free-hand annotation JSON)
```

`GradeType` enum on `Question`: `DIRECT`, `POSITIVE`, `NEGATIVE`, `HYBRID`.
