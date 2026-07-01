# Workload Scenarios — Sequence Diagrams

These diagrams model the three Locust scenarios under `lab/scenarios/`.
Render with any Mermaid viewer (VS Code, GitHub, mermaid.live).

---

## S01 — Browse (read-only baseline)

Simulates a teacher reviewing exam results. Pure read path, no mutations.
**50 concurrent users · think-time 1–3 s · task weights: courses×5, exams×4, exam_detail×4, sheets×3, questions×3, students×2, responses×2, final_results×1**

```mermaid
sequenceDiagram
    autonumber
    actor Teacher
    participant Locust
    participant API as Quarkus API :8082
    participant DB as MySQL

    Note over Locust,DB: on_start() — runs once per virtual user

    Teacher->>Locust: spawn virtual user
    Locust->>API: POST /api/authenticate {admin/admin}
    API->>DB: SELECT user WHERE login='admin'
    DB-->>API: user row + roles
    API-->>Locust: 200 {id_token: JWT}
    Locust->>Locust: store Bearer token in headers

    Locust->>API: GET /api/exams?page=0&size=50
    API->>DB: SELECT * FROM exam (paginated)
    DB-->>API: exam list
    API-->>Locust: 200 [exam objects]
    Locust->>Locust: cache exam_ids[]

    Note over Locust,DB: task loop (repeats with 1–3 s think-time)

    loop Steady-state task loop
        alt weight=5 · list_courses
            Locust->>API: GET /api/courses?page=0&size=20
            API->>DB: SELECT * FROM course
            DB-->>API: course list
            API-->>Locust: 200 [courses]
        else weight=4 · list_exams
            Locust->>API: GET /api/exams?page=0&size=50
            API->>DB: SELECT * FROM exam
            DB-->>API: exam list
            API-->>Locust: 200 [exams]
            Locust->>Locust: refresh exam_ids[]
        else weight=4 · exam_detail
            Locust->>API: GET /api/exams/{id}
            API->>DB: SELECT * FROM exam WHERE id=?
            DB-->>API: exam row
            API-->>Locust: 200 {exam}
        else weight=3 · list_exam_sheets
            Locust->>API: GET /api/exam-sheets?examId={id}
            API->>DB: SELECT * FROM exam_sheet WHERE exam_id=?
            DB-->>API: sheet list
            API-->>Locust: 200 [sheets]
            Locust->>Locust: cache sheet_ids[]
        else weight=3 · list_questions
            Locust->>API: GET /api/questions?examId={id}
            API->>DB: SELECT * FROM question WHERE exam_id=?
            DB-->>API: question list
            API-->>Locust: 200 [questions]
        else weight=2 · list_students
            Locust->>API: GET /api/students?page=0&size=50
            API->>DB: SELECT * FROM student
            DB-->>API: student list
            API-->>Locust: 200 [students]
        else weight=2 · list_student_responses
            Locust->>API: GET /api/student-responses?examSheetId={id}
            API->>DB: SELECT * FROM student_response WHERE sheet_id=?
            DB-->>API: response list
            API-->>Locust: 200 [responses]
        else weight=1 · list_final_results
            Locust->>API: GET /api/final-results?examId={id}
            API->>DB: SELECT * FROM final_result WHERE exam_id=?
            DB-->>API: result list
            API-->>Locust: 200 [results]
        end
        Locust->>Locust: sleep(uniform(1, 3) s)
    end
```

---

## S02 — Grade (write-heavy)

Simulates a teacher actively grading student responses. Adds write path:
`PUT /api/student-responses` triggers InnoDB row update + WAL flush + `canAccess()` DB check.
**50 concurrent graders · think-time 1–4 s · task weights: grade×4, view_sheet×4, check_progress×2, switch_question×1**

```mermaid
sequenceDiagram
    autonumber
    actor Teacher
    participant Locust
    participant API as Quarkus API :8082
    participant Auth as SecurityIdentity
    participant DB as MySQL / InnoDB

    Note over Locust,DB: on_start() — context discovery

    Teacher->>Locust: spawn virtual user
    Locust->>API: POST /api/authenticate
    API->>DB: SELECT user (login + roles)
    DB-->>API: user row
    API-->>Locust: 200 {id_token}

    Locust->>API: GET /api/exams?page=0&size=20  [name: setup]
    API->>DB: SELECT exam (paginated)
    DB-->>API: exam list
    API-->>Locust: 200 → picks random exam_id

    Locust->>API: GET /api/exam-sheets?examId={id}  [name: setup]
    API->>DB: SELECT sheet WHERE exam_id=?
    DB-->>API: sheet list (up to 200)
    API-->>Locust: 200 → caches sheet_ids[]

    Locust->>API: GET /api/questions?examId={id}  [name: setup]
    API->>DB: SELECT question WHERE exam_id=?
    DB-->>API: question list
    API-->>Locust: 200 → caches question_ids[]

    loop Pre-load 3 random sheets
        Locust->>API: GET /api/student-responses?examSheetId={id}  [name: setup]
        API->>DB: SELECT student_response WHERE sheet_id=?
        DB-->>API: response list
        API-->>Locust: 200 → responses[sheet_id] = [...]
    end

    Note over Locust,DB: task loop (repeats with 1–4 s think-time)

    loop Steady-state grading loop
        alt weight=4 · grade_response  ← THE WRITE HOT PATH
            Locust->>Locust: pick random response from cache, generate note∈[0,20]
            Locust->>API: PUT /api/student-responses {id, note, questionId, sheetId, ...}
            API->>Auth: canAccess(studentResponse, user)?
            Auth->>DB: SELECT student_response JOIN exam JOIN course WHERE owner=user
            DB-->>Auth: ownership confirmed
            Auth-->>API: ✓ authorized
            API->>DB: UPDATE student_response SET note=? WHERE id=?
            Note right of DB: InnoDB row lock acquired<br/>WAL flush (fsync)<br/>Row lock released
            DB-->>API: updated row
            API-->>Locust: 200 {updated response}
            Locust->>Locust: update local cache note
        else weight=4 · view_sheet
            Locust->>API: GET /api/student-responses?examSheetId={id}
            API->>DB: SELECT student_response WHERE sheet_id=?
            DB-->>API: response list
            API-->>Locust: 200 → refresh cache
        else weight=2 · check_progress
            Locust->>API: GET /api/exam-sheets?examId={id}
            API->>DB: SELECT exam_sheet WHERE exam_id=? (size=200)
            DB-->>API: sheet list with grading status
            API-->>Locust: 200 [sheets]
        else weight=1 · switch_question
            Locust->>API: GET /api/questions?examId={id}
            API->>DB: SELECT question WHERE exam_id=?
            DB-->>API: question list
            API-->>Locust: 200 → refresh question_ids[]
        end
        Locust->>Locust: sleep(uniform(1, 4) s)
    end
```

---

## S03 — Mixed (Grid5000 reference workload)

**60 % BrowseUser (weight=3) + 40 % GraderUser (weight=2)** — the scenario used
for the provisioning experiments on Grid5000. Both user types share the same `on_start()`
context-discovery sequence; they then diverge into their respective task loops.

```mermaid
sequenceDiagram
    autonumber
    participant Locust as Locust Master
    participant BU as BrowseUser (×30 at 50 users)
    participant GU as GraderUser (×20 at 50 users)
    participant API as Quarkus API :8082
    participant DB as MySQL

    Note over Locust,DB: Spawn phase  (5 users/s ramp → 50 users)

    Locust->>BU: spawn 30 BrowseUsers (weight=3)
    Locust->>GU: spawn 20 GraderUsers (weight=2)

    par BrowseUser on_start
        BU->>API: POST /api/authenticate
        API->>DB: SELECT user
        DB-->>API: user row
        API-->>BU: JWT token
        BU->>API: GET /api/exams (setup)
        API-->>BU: exam list → cache exam_ids[]
        BU->>API: GET /api/exam-sheets (setup)
        API-->>BU: sheet list → cache sheet_ids[]
        BU->>API: GET /api/questions (setup)
        API-->>BU: question list → cache question_ids[]
    and GraderUser on_start
        GU->>API: POST /api/authenticate
        API->>DB: SELECT user
        DB-->>API: user row
        API-->>GU: JWT token
        GU->>API: GET /api/exams (setup)
        API-->>GU: exam list → cache exam_ids[]
        GU->>API: GET /api/exam-sheets (setup)
        API-->>GU: sheet list → cache sheet_ids[]
        GU->>API: GET /api/questions (setup)
        API-->>GU: question list
        GU->>API: GET /api/student-responses × 3 (pre-load)
        API-->>GU: response cache seeded
    end

    Note over Locust,DB: Steady-state  (50 users, ~22 req/s measured)

    par BrowseUser task loop  [read-only · think 1–3 s]
        loop
            BU->>API: GET /api/courses       [w=5]
            BU->>API: GET /api/exams         [w=4]
            BU->>API: GET /api/exams/{id}    [w=4]
            BU->>API: GET /api/exam-sheets   [w=3]
            BU->>API: GET /api/questions     [w=3]
            BU->>API: GET /api/students      [w=2]
            BU->>API: GET /api/student-responses [w=2]
            BU->>API: GET /api/final-results [w=1]
            Note right of BU: All reads → MySQL SELECTs<br/>Hikari pool shared with GU
        end
    and GraderUser task loop  [write-heavy · think 1–4 s]
        loop
            GU->>API: PUT /api/student-responses (grade) [w=4]
            Note right of GU: canAccess() SELECT<br/>+ InnoDB UPDATE + WAL flush<br/>→ disk I/O + lock contention
            GU->>API: GET /api/student-responses (load sheet) [w=4]
            GU->>API: GET /api/exam-sheets (progress) [w=2]
            GU->>API: GET /api/questions (switch) [w=1]
        end
    end

    Note over Locust,DB: Bottleneck: Hikari pool max-size=8 shared by 50 users<br/>→ bimodal latency (p50≈9 ms, p95≈33 ms, p99≈100 ms)
```

---

## Scenario comparison table

| Dimension         | S01 Browse         | S02 Grade            | S03 Mixed (reference) |
|-------------------|--------------------|----------------------|-----------------------|
| User type         | BrowseUser only    | GraderUser only      | 60% Browse / 40% Grade|
| HTTP methods      | GET only           | GET + PUT            | GET + PUT             |
| DB operations     | SELECT only        | SELECT + UPDATE      | SELECT + UPDATE       |
| Disk I/O          | Buffer pool reads  | WAL flush per grade  | WAL flush (40% writes)|
| Lock contention   | None               | InnoDB row locks     | Row locks (40%)       |
| Sensitive IaC params | RAM (buffer pool) | Disk IOPS, vCPU, RAM | All three             |
| Grid5000 role     | Baseline reference | Write-path stress    | **Main experiment**   |
