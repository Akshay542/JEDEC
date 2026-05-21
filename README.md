# Package Reliability Qualification Suite

JEDEC reliability test calculator and qualification report generator. Computes
sample sizes and demonstrated reliability for the JEDEC qualification test
matrix using the chi-squared method, then renders a printable PDF
qualification report.

## Run it

```
pip install -r requirements.txt
python3 jedec_web.py
```

The web UI starts on `http://localhost:5001` (or the port set in `$PORT`)
and auto-opens in your default browser when launched on `5001`. The app is
single-process Tornado, in-memory sessions only — restarting the server
clears all state.

## Layout

```
JEDEC/
├── jedec_calc.py         # pure-stdlib calculation engine
├── jedec_web.py          # Tornado web UI + PDF report generator
├── requirements.txt      # tornado, reportlab, Pillow
├── specs/                # JESD reference PDFs served at /specs/
└── sample_qual_report_TTV.pdf   # example output (untracked)
```

`jedec_calc.py` is the calculation engine — pure stdlib, no scientific
libraries. It implements the regularized lower incomplete gamma function via
series expansion / Lentz continued fraction, then the chi-squared CDF/PPF on
top, then the JEDEC sample-size formulas. Importable on its own.

`jedec_web.py` wraps the engine in a Tornado app: part-type selection
(active device vs thermal test vehicle), test-matrix configuration, sample
size + pass/fail evaluation, and a ReportLab PDF report covering the full
qualification matrix.

## Tests covered

Aligned to the corresponding JEDEC standard, served locally as the linked
PDF:

| Test         | Standard           | What it checks                       |
|--------------|--------------------|--------------------------------------|
| Preconditioning (MSL) | JESD22-A113I / J-STD-020F | moisture-sensitivity preconditioning |
| uHAST        | JESD22-A118B       | unbiased HAST (humidity / temp)      |
| THB          | JESD22-A110        | temperature humidity bias            |
| TC           | JESD22-A104F       | temperature cycling                  |
| TSHOCK       | JESD22-A106B       | thermal shock                        |
| MSHOCK       | JESD22-B110B       | mechanical shock                     |
| VIB          | JESD22-B103B       | vibration                            |
| PC           | JESD22-A122        | power cycling                        |
| PTC          | JESD22-A105D       | power-temperature cycling            |
| HTS          | JESD22-A103D       | high-temperature storage             |
| Shadow Moiré | JESD22-B112C       | warpage measurement                  |
| HTOL         | JESD22-A108G       | high-temperature operating life      |
| ELFR         | JESD74A            | early-life failure rate              |
| ESD-CDM      | JS-002             | charged-device model ESD             |
| ESD-HBM      | JS-001             | human-body model ESD                 |
| Latch-up     | JESD78E            | CMOS latch-up immunity               |

## Sample size methodology

The JEDEC-standard sample-size derivation, implemented in
`jedec_calc.min_sample_size`:

```
n = chi²(2k+2, C) / (2 · |ln R|)
```

where R is the required reliability, C the confidence level, and k the
allowed-failure count. Demonstrated reliability after a test
(`demonstrated_reliability`):

```
R = exp( -chi²(2k+2, C) / (2n) )
```

Both are pure-stdlib implementations using the regularized incomplete gamma
function — no SciPy dependency.

## Part types

The test matrix conditionally activates depending on whether the device
under qualification is an active part or a thermal test vehicle (TTV).
TTVs skip the bias-driven tests (HTOL, ELFR, ESD, latch-up). Selectable in
the UI via `PART_TYPE_LABELS` in `jedec_calc.py`.

## Notes

- Deployment: the project previously shipped a `Dockerfile` and
  `railway.json` for one-click Railway deploy; both were removed. The app
  runs equally well as a plain `python3 jedec_web.py` against any
  Python 3.8+ interpreter with the three deps installed.
- Sessions are in-memory only and tied to a 1-day signed cookie — fine for
  single-user / desktop use, not for multi-tenant production.
- `specs/` PDFs are JEDEC standards documents served as static assets. They
  are checked into the repo so the linked references render offline.
