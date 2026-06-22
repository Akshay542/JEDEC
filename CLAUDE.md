# CLAUDE.md — Package Reliability Qualification Suite

Diamond Foundry internal tool for JEDEC semiconductor package reliability
qualification. Computes sample sizes, evaluates pass/fail results, tracks
test schedules, and generates printable PDF qualification reports.

## Running the app

```bash
pip install -r requirements.txt
python3 jedec_web.py
```

Starts on `http://localhost:5001` and auto-opens the browser. Port is
overridable via `$PORT`. `openpyxl` is auto-installed at startup if absent
(system pip → `--user` fallback). Restarting the server does **not** clear
project data — that persists in `jedec_projects.db` (SQLite).

## File layout

```
jedec_calc.py     Pure-stdlib calculation engine + standalone CLI
jedec_web.py      Tornado web app, HTML rendering, PDF/Excel export
jedec_db.py       SQLite persistence layer (jedec_projects.db)
requirements.txt  tornado, reportlab, Pillow  (openpyxl auto-installed)
specs/            JEDEC standard PDFs served statically at /specs/
```

## Architecture

### jedec_calc.py
- Chi-squared CDF/PPF — pure stdlib via regularised lower incomplete gamma
  function (series expansion for x < a+1, Lentz continued-fraction otherwise).
  Wilson-Hilferty approximation for warm start; bisection refinement to 1e-9.
- `min_sample_size(R, C, k)` — JEDEC formula `n = χ²(2k+2, C) / (2·|ln R|)`
- `demonstrated_reliability(n, k, C)` — inverse: `R = exp(−χ²(2k+2,C) / 2n)`
- `min_sample_size_ltpd(ltpd_pct, k)` — JESD47I §3.8 formula at 90% CL
- `TABLE_A` / `TABLE_A_LTPD` — JESD47I Table A lookup (0–12 failures, 7 LTPD levels)
- `TESTS` dict — 16 JEDEC tests with standard, condition, duration, sample size,
  pass criteria, pre/post testing, destructive flag, active_devices flag.
- `PRECOND` dict — Preconditioning (MSL) definition, universal precursor.
- `PART_TYPE_LABELS` — `"active"`, `"ttv"`, `"die"`. TTV excludes bias-driven
  tests; Die further excludes Power Cycling and Shadow Moiré.
- Also contains a standalone terminal CLI (not used by the web app).

### jedec_web.py
Single-file Tornado app, ~6000 lines. All HTML is rendered as Python f-strings
(no template files). Bootstrap 5 + Bootstrap Icons for styling.

**URL routes:**

| Route | Handler | Purpose |
|---|---|---|
| `/` | IndexHandler | Home / dashboard |
| `/lookup` | LookupHandler | Test condition reference (read-only) |
| `/sample-size` | SampleSizeHandler | Stateless sample size planner |
| `/pass-fail` | PassFailHandler | Stateless pass/fail evaluator |
| `/report` | ReportHandler | Stateless qual report builder |
| `/report/pdf` | ReportPdfHandler | PDF download (ReportLab) |
| `/projects` | ProjectListHandler | Project list + create |
| `/projects/<id>` | ProjectDetailHandler | Project overview / test status |
| `/projects/<id>/meta` | ProjectMetaHandler | Edit device metadata |
| `/projects/<id>/sample-size` | ProjectSampleSizeHandler | Per-project sample size |
| `/projects/<id>/pass-fail` | ProjectPassFailHandler | Per-project pass/fail |
| `/projects/<id>/report` | ProjectReportHandler | Per-project qual report |
| `/projects/<id>/report/pdf` | ProjectReportPdfHandler | Per-project PDF export |
| `/projects/<id>/csam` | ProjectCsamHandler | CSAM image gallery + upload |
| `/projects/<id>/csam/<iid>` | ProjectCsamImageHandler | Full-size CSAM image |
| `/projects/<id>/csam/<iid>/thumb` | ProjectCsamThumbHandler | Thumbnail |
| `/projects/<id>/csam/<iid>/delete` | ProjectCsamDeleteHandler | Delete image |
| `/projects/<id>/tracker` | ProjectTrackerHandler | Gantt schedule tracker |
| `/projects/<id>/tracker/xlsx` | ProjectTrackerXlsxHandler | Excel export |
| `/projects/<id>/tracker/task/<tid>/<edit\|delete>` | ProjectTaskHandler | Edit/delete Gantt task |
| `/specs/<file>` | StaticFileHandler | Serve spec PDFs |

**Sessions:** 1-day signed cookie (`sid`) → in-memory `SESSIONS` dict. Session
stores only `part_type` and `last_report` for the stateless pages. All project
data is in SQLite — sessions are not required for project persistence.

**Part-type filtering:** `applicable_tests(part_type)` in `jedec_web.py`
(mirrors `jedec_calc.py`). Active → all 16 tests. TTV → tests with
`active_devices=False`. Die → TTV subset minus `pc` and `shadow_moire`.

**Condition tables defined in jedec_web.py:**
- `TC_CONDITIONS` — 13 conditions (A–T) per JESD22-A104F Tables 1, 3, 4
- `TSHOCK_CONDITIONS` — 4 conditions (A–D) per JESD22-A106B
- `UHAST_CONDITIONS` — 2 conditions (A–B) per JESD22-A118B
- `THB_CONDITIONS` — 2 conditions (A–B) per JESD22-A110
- `MSHOCK_CONDITIONS` — 8 service conditions (A–H) per JESD22-B110B
- `VIB_SIN_CONDITIONS` — 8 sinusoidal conditions per JESD22-B103B Table 1
- `VIB_RAN_CONDITIONS` — 9 random conditions (A–I) per JESD22-B103B Table 2
- `PC_CONDITIONS` — 5 conditions (A–E) per JESD22-A122
- `PTC_CONDITIONS` — 2 conditions (A–B) per JESD22-A105D
- `HTS_CONDITIONS` — 6 conditions (A–F) per JESD22-A103D

**PDF generation:** ReportLab `SimpleDocTemplate` on A4. Covers full qual
matrix: test conditions, sample counts, CSAM pass/fail, statistical details,
and JESD47I Table A lookup. Generated by `ProjectReportPdfHandler`.

**Excel export:** openpyxl workbook for the Gantt/schedule tracker. Columns
include task name, category, test key, start week, duration, sample count,
status. Generated by `ProjectTrackerXlsxHandler`.

### jedec_db.py
SQLite (WAL mode, foreign keys on). DB file: `jedec_projects.db` beside the
script. Schema initialized and incrementally migrated by `init_db()` at startup.

**Tables:**

| Table | Purpose |
|---|---|
| `projects` | id, name, description, part_type, status, timestamps |
| `project_meta` | device_name, device_pkg, bond_type, engineer, lot_id, notes, gantt_start_date |
| `project_sample_size` | ltpd, failures, confidence — planner inputs |
| `project_pass_fail` | n, failures, confidence, ltpd_pct — pass/fail inputs |
| `project_samples` | JSON blob mapping test_key → sample count |
| `project_gantt` | Gantt tasks: task_name, category, test_key, start_week, duration, status, sort_order, n_mode, n_custom, parent_task_id |
| `project_gantt_history` | Undo stack (max 20 entries per project) with action_type + JSON snapshot |
| `project_csam` | CSAM images as base64 strings: sample_id, test_key, stage, filename, notes |
| `project_test_conditions` | Per-project selected condition per test (e.g. TC → "H") |
| `project_test_tracker` | Test start/complete timestamps and duration_hours per test |

**Key migration note:** In `project_gantt`, Preconditioning uses `test_key='precond'`
(not `'pc'`, which is Power Cycling). A one-time migration in `init_db()` renames
any old `'pc'` rows in category `'Stress'` to `'precond'`.

## Tests covered

| Key | Name | Standard |
|---|---|---|
| `uhast` | uHAST | JESD22-A118B |
| `tc` | Temperature Cycling | JESD22-A104F |
| `tshock` | Thermal Shock | JESD22-A106B |
| `mshock` | Mechanical Shock | JESD22-B110B |
| `vib` | Variable Freq Vibration | JESD22-B103B |
| `pc` | Power Cycling | JESD22-A122 |
| `ptc` | Power & Temperature Cycling | JESD22-A105D |
| `hts` | High Temperature Storage | JESD22-A103D |
| `shadow_moire` | Shadow Moiré (warpage) | JESD22-B112C |
| `htol` | HTOL | JESD22-A108G / JESD85 |
| `elfr` | ELFR | JESD22-A108G / JESD74A |
| `thb` | Temp Humidity Bias | JESD22-A110 |
| `esd_cdm` | ESD — CDM | JS-002 |
| `esd_hbm` | ESD — HBM | JS-001 |
| `latchup` | Latch-Up | JESD78F |

Preconditioning (MSL) is a universal precursor stored separately as `PRECOND`
(not in `TESTS`), referenced by key `'precond'` in Gantt/tracker.

## CSAM threshold

95% bond area (`CSAM_THRESHOLD = 95.0`). Evaluation stages: pre-PC, post-PC,
post-test. Failure at any stage blocks progress to the next.

## Common patterns

- All HTML is generated in Python f-strings; search by handler class name to
  find the relevant HTML block.
- `_page(active, part_type, body, title, project, active_sub)` builds the full
  page shell with top-nav and optional project sub-nav.
- `Base.emit(body, title, active, project, active_sub)` is the standard
  response method — reads session, calls `_page`, writes response.
- Gantt seed (`action="seed"`) auto-generates tasks from saved sample counts
  via `_compute_seeded_tasks(sample_counts)`. Only runs when the project has no
  existing Gantt tasks.
- Undo (`action="undo"`) pops from `project_gantt_history` and replays the
  inverse operation (re-insert deleted task, restore edited task state, etc.).
