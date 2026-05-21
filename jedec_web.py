#!/usr/bin/env python3
"""
Package Reliability Qualification Suite
JEDEC reliability test calculator and qualification report generator.

Run:  python3 jedec_web.py
Open: http://localhost:5000

Requires: tornado (stdlib-only otherwise — reportlab for PDF)
"""

from __future__ import annotations
import os, sys, math, io, uuid, webbrowser, base64, hashlib
from datetime import datetime

# ── Compatibility patch: Python 3.8 + macOS OpenSSL rejects usedforsecurity ──
_orig_md5 = hashlib.md5
def _patched_md5(*args, **kwargs):
    kwargs.pop("usedforsecurity", None)
    return _orig_md5(*args, **kwargs)
hashlib.md5 = _patched_md5

import tornado.ioloop
import tornado.web

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jedec_calc import (TESTS, PRECOND, PART_TYPE_LABELS,
                        min_sample_size, demonstrated_reliability, pass_fail as _pf,
                        min_sample_size_ltpd, TABLE_A, TABLE_A_LTPD)
import jedec_db as _db

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, HRFlowable, KeepTogether, PageBreak,
                                Image as RLImage)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

# ── Constants ──────────────────────────────────────────────────────────────────
RTD_SENSORS    = [f"T{i}" for i in range(1, 17)]
BOND_TYPES     = ["Cu TCB", "Ag Sinter", "Ag TCB"]
CSAM_THRESHOLD = 95.0

# ── JEDEC spec document URLs — served locally from /specs/ ────────────────────
SPEC_URLS = {
    "uhast":       "/specs/JESD22-A118B.pdf",
    "tc":          "/specs/JESD22-A104F.pdf",
    "tshock":      "/specs/JESD22-A106B.pdf",
    "mshock":      "/specs/JESD22-B110B.pdf",
    "vib":         "/specs/JESD22-B103B.pdf",
    "pc":          "/specs/JESD22-A122.pdf",
    "ptc":         "/specs/JESD22-A105D.pdf",
    "hts":         "/specs/JESD22-A103D.pdf",
    "shadow_moire":"/specs/JESD22-B112C.pdf",
    "htol":        "/specs/JESD22-A108G.pdf",
    "elfr":        "/specs/JESD74A.pdf",
    "thb":         "/specs/JESD22-A110.pdf",
    "esd_cdm":     "/specs/JS-002.pdf",
    "esd_hbm":     "/specs/JS-001.pdf",
    "latchup":     "/specs/JESD78E.pdf",
    "jesd47":      "/specs/JESD47I.pdf",
}

# ── TC condition table (JESD22-A104F Table 1 & Table 3) ───────────────────────
# keys: condition letter → tmin, tmax (°C), typical cycles/hr, applicable soak modes
# Soak mode min times: 1→1 min, 2→5 min, 3→10 min, 4→15 min
# t4: Table 4 (solder interconnect) cycle rates keyed by soak mode number (str)
# Conditions not in Table 4 have t4={}; display falls back to Table 3 "cycles" value.
TC_CONDITIONS = {
    "A": {"tmin": -55, "tmax":  85, "cycles": "2–3",   "soak": [1, 2, 3],    "t4": {}},
    "B": {"tmin": -55, "tmax": 125, "cycles": "2–3",   "soak": [1, 2],       "t4": {}},
    "C": {"tmin": -65, "tmax": 150, "cycles": "2",     "soak": [1, 2],       "t4": {}},
    "G": {"tmin": -40, "tmax": 125, "cycles": "<1–2",  "soak": [1, 2, 3, 4], "t4": {"2":"2","3":"\u22642","4":"<1"}},
    "H": {"tmin": -55, "tmax": 150, "cycles": "2",     "soak": [1, 2],       "t4": {}},
    "I": {"tmin": -40, "tmax": 115, "cycles": "1–2",   "soak": [1, 2, 3, 4], "t4": {"2":"2","3":"\u22642","4":"<1"}},
    "J": {"tmin":   0, "tmax": 100, "cycles": "1–3",   "soak": [1, 2, 3, 4], "t4": {"2":"2","3":"\u22642","4":"<1"}},
    "K": {"tmin":   0, "tmax": 125, "cycles": "1–3",   "soak": [1, 2, 3, 4], "t4": {"2":"2","3":"\u22642","4":"<1"}},
    "L": {"tmin": -55, "tmax": 110, "cycles": "1–3",   "soak": [1, 2, 3, 4], "t4": {"2":"2","3":"\u22642","4":"<1"}},
    "M": {"tmin": -40, "tmax": 150, "cycles": "1–3",   "soak": [1, 2, 3, 4], "t4": {}},
    "N": {"tmin": -40, "tmax":  85, "cycles": "1–3",   "soak": [1, 2, 3],    "t4": {}},
    "R": {"tmin": -25, "tmax": 125, "cycles": "1–2",   "soak": [1, 2],       "t4": {"1":"2"}},
    "T": {"tmin": -40, "tmax": 100, "cycles": "1–2",   "soak": [3, 4],       "t4": {"3":"\u22642","4":"<1"}},
}
TC_SOAK_TIMES = {1: 1, 2: 5, 3: 10, 4: 15}  # mode → min soak time (minutes)

# ── T-Shock condition table (JESD22-A106B Table 1) ────────────────────────────
# Step 1 = hot bath, Step 2 = cold bath; tolerances ±10/0 & 0/−10 respectively
# fluid_s1: recommended fluid for Step 1 (hot); fluid_s2: always Perfluorocarbon
TSHOCK_CONDITIONS = {
    "A": {"hot":  85, "cold": -40, "fluid_s1": "Water or Perfluorocarbon", "fluid_s2": "Perfluorocarbon"},
    "B": {"hot": 100, "cold":   0, "fluid_s1": "Perfluorocarbon",          "fluid_s2": "Perfluorocarbon"},
    "C": {"hot": 125, "cold": -55, "fluid_s1": "Perfluorocarbon",          "fluid_s2": "Perfluorocarbon"},
    "D": {"hot": 150, "cold": -65, "fluid_s1": "Perfluorocarbon",          "fluid_s2": "Perfluorocarbon"},
}

# ── UHAST condition table (JESD22-A118B Table 1) ──────────────────────────────
# temp_db: dry-bulb °C (±2); rh: relative humidity % (±5)
# temp_wb: wet-bulb °C; vp_kpa / vp_psia: vapor pressure
# duration: typical test duration per condition
UHAST_CONDITIONS = {
    "A": {"temp_db": 130, "rh": 85, "temp_wb": 124.7, "vp_kpa": 230,  "vp_psia": 33.3, "duration": "96 hours (−0, +2)"},
    "B": {"temp_db": 110, "rh": 85, "temp_wb": 105.2, "vp_kpa": 122,  "vp_psia": 17.7, "duration": "264 hours (−0, +2)"},
}

# ── M-Shock condition table (JESD22-B110B Table 1 — free state test levels) ───
# accel_g: acceleration peak (g); pulse_ms: half-sine pulse duration (ms)
# vel_cms / vel_ins: velocity change; drop_cm / drop_in: equivalent drop height
MSHOCK_CONDITIONS = {
    "H": {"accel_g": 2900, "pulse_ms": 0.3, "vel_cms": 543,  "vel_ins": 214,  "drop_cm": 150,  "drop_in": 59},
    "G": {"accel_g": 2000, "pulse_ms": 0.4, "vel_cms": 499,  "vel_ins": 197,  "drop_cm": 127,  "drop_in": 50},
    "B": {"accel_g": 1500, "pulse_ms": 0.5, "vel_cms": 468,  "vel_ins": 184,  "drop_cm": 112,  "drop_in": 44},
    "F": {"accel_g":  900, "pulse_ms": 0.7, "vel_cms": 393,  "vel_ins": 155,  "drop_cm":  78.9,"drop_in": 31},
    "A": {"accel_g":  500, "pulse_ms": 1.0, "vel_cms": 312,  "vel_ins": 123,  "drop_cm":  49.7,"drop_in": 20},
    "E": {"accel_g":  340, "pulse_ms": 1.2, "vel_cms": 255,  "vel_ins": 100,  "drop_cm":  33.1,"drop_in": 13},
    "D": {"accel_g":  200, "pulse_ms": 1.5, "vel_cms": 187,  "vel_ins":  73.7,"drop_cm":  17.9,"drop_in":  7},
    "C": {"accel_g":  100, "pulse_ms": 2.0, "vel_cms": 125,  "vel_ins":  49.2,"drop_cm":   7.9,"drop_in":  3},
}

# ── Vibration condition tables (JESD22-B103B) ─────────────────────────────────
# Table 1 — Sinusoidal component test levels (conditions 1–8)
VIB_SIN_CONDITIONS = {
    "1": {"accel_g": 20,    "disp_in": 0.060,   "disp_mm": 1.5,    "xover_hz": 80,  "fmin_hz": 20, "fmax_hz": 2000},
    "2": {"accel_g": 10,    "disp_in": 0.040,   "disp_mm": 1.0,    "xover_hz": 70,  "fmin_hz": 10, "fmax_hz": 1000},
    "3": {"accel_g":  3,    "disp_in": 0.030,   "disp_mm": 0.75,   "xover_hz": 45,  "fmin_hz":  5, "fmax_hz":  500},
    "4": {"accel_g":  1,    "disp_in": 0.020,   "disp_mm": 0.5,    "xover_hz": 31,  "fmin_hz":  5, "fmax_hz":  500},
    "5": {"accel_g":  0.3,  "disp_in": 0.010,   "disp_mm": 0.25,   "xover_hz": 24,  "fmin_hz":  5, "fmax_hz":  500},
    "6": {"accel_g":  0.1,  "disp_in": 0.005,   "disp_mm": 0.125,  "xover_hz": 20,  "fmin_hz":  5, "fmax_hz":  500},
    "7": {"accel_g":  0.01, "disp_in": 0.001,   "disp_mm": 0.039,  "xover_hz": 14,  "fmin_hz":  5, "fmax_hz":  500},
    "8": {"accel_g":  0.001,"disp_in": 0.0005,  "disp_mm": 0.0127, "xover_hz":  6.2,"fmin_hz":  5, "fmax_hz":  500},
}
# Table 2 — Random vibration overall test levels (conditions A–I)
# rms_g: RMS acceleration; vel_ins: RMS velocity; disp_in: RMS displacement; sigma_in: 6×RMS displacement (3σ pk-pk)
VIB_RAN_CONDITIONS = {
    "A": {"rms_g": 6.27,   "vel_ins": 29.0,  "disp_in": 0.926,   "sigma_in": 5.55},
    "B": {"rms_g": 3.10,   "vel_ins": 13.2,  "disp_in": 0.426,   "sigma_in": 2.56},
    "C": {"rms_g": 1.24,   "vel_ins":  5.22, "disp_in": 0.178,   "sigma_in": 1.07},
    "D": {"rms_g": 1.11,   "vel_ins":  1.64, "disp_in": 0.0310,  "sigma_in": 0.186},
    "E": {"rms_g": 0.686,  "vel_ins":  0.703,"disp_in": 0.00543, "sigma_in": 0.0326},
    "F": {"rms_g": 0.416,  "vel_ins":  0.425,"disp_in": 0.00355, "sigma_in": 0.0213},
    "G": {"rms_g": 0.246,  "vel_ins":  0.215,"disp_in": 0.00171, "sigma_in": 0.0102},
    "H": {"rms_g": 0.123,  "vel_ins":  0.113,"disp_in": 0.000832,"sigma_in": 0.00499},
    "I": {"rms_g": 0.0626, "vel_ins":  0.0589,"disp_in":0.000395,"sigma_in": 0.002237},
}

# ── Power Cycling condition table (JESD22-A122 Table 2) ───────────────────────
# tmin / tmax: Tcycle(min/max) in °C; tolerance ±5°C on both
# delta_t: nominal ΔT = tmax − tmin
# Typical cycle rate: 2–6 cycles/hr (NOTE 2)
PC_CONDITIONS = {
    "A": {"tmin":  25, "tmax": 100, "delta_t":  75},
    "B": {"tmin":  25, "tmax": 125, "delta_t": 100},
    "C": {"tmin":  10, "tmax": 100, "delta_t":  90},
    "D": {"tmin":  10, "tmax": 125, "delta_t": 115},
    "E": {"tmin":  40, "tmax": 100, "delta_t":  60},
}
# Table 1: method combinations (power × cooling)
PC_METHODS = [
    ("Constant Power / Constant Cooling",  "Easiest to implement; requires careful test detail"),
    ("Constant Power / Variable Cooling",  "Best for functional devices & tightly controlled heater resistance; allows Tj variation modeling"),
    ("Variable Power / Constant Cooling",  "Closely matches actual product profile; requires surge control"),
    ("Variable Power / Variable Cooling",  "Most flexible but most complex; best for devices with complex chip power maps"),
]

# ── HTS condition table (JESD22-A103D Table 1) ────────────────────────────────
# temp_c: storage temperature °C; tolerance −0/+10°C
# JESD47 default duration: 1000 hours at Condition B
HTS_CONDITIONS = {
    "A": {"temp_c": 125},
    "B": {"temp_c": 150},
    "C": {"temp_c": 175},
    "D": {"temp_c": 200},
    "E": {"temp_c": 250},
    "F": {"temp_c": 300},
}

# ── In-memory sessions ─────────────────────────────────────────────────────────
SESSIONS: dict = {}

COOKIE_SECRET = "jedec-df-calc-2025-secret-key-static"

def _get_or_create(handler):
    raw = handler.get_secure_cookie("sid")
    if raw:
        sid = raw.decode()
        if sid in SESSIONS:
            return sid, SESSIONS[sid]
    sid = str(uuid.uuid4())
    SESSIONS[sid] = {"part_type": "ttv", "last_report": None}
    handler.set_secure_cookie("sid", sid, expires_days=1)
    return sid, SESSIONS[sid]

# ── Helpers ────────────────────────────────────────────────────────────────────

def _csam_eval(before_pc, after_pc, after_test):
    """
    Evaluate CSAM bonded area against threshold (95%).
    Returns (status_text, badge_class) tuple.
    """
    if before_pc is None:
        return ("No data", "secondary")
    if before_pc < CSAM_THRESHOLD:
        return ("Rejected — pre-PC < 95%", "danger")
    if after_pc is None:
        return ("Awaiting post-PC", "warning")
    if after_pc < CSAM_THRESHOLD:
        return ("Fail — Preconditioning", "danger")
    if after_test is None:
        return ("Awaiting post-test", "warning")
    if after_test < CSAM_THRESHOLD:
        return ("Fail — Post-test CSAM", "danger")
    return ("Pass", "success")

def applicable_tests(part_type: str) -> dict:
    if part_type == "ttv":
        return {k: v for k, v in TESTS.items() if not v["active_devices"]}
    return TESTS

STATUS_OPTS = [
    ("ns", "Not Started",      "secondary"),
    ("ip", "In Progress",      "warning"),
    ("co", "Complete",         "success"),
    ("na", "N/A",              "light"),
]
# Status options for characterization-only tests (no Pass/Fail)
STATUS_OPTS_CHAR = [
    ("ns", "Not Started",      "secondary"),
    ("ip", "In Progress",      "warning"),
    ("ch", "Characterized",    "info"),
    ("na", "N/A",              "light"),
]
STATUS_LABEL = {s[0]: s[1].upper() for s in STATUS_OPTS + STATUS_OPTS_CHAR}
STATUS_COLOR = {s[0]: s[2] for s in STATUS_OPTS + STATUS_OPTS_CHAR}

# ── Base HTML Layout ───────────────────────────────────────────────────────────

def _page(active: str, part_type: str, body: str, title: str = "Package Reliability",
          project: dict = None, active_sub: str = "") -> str:
    top_nav_links = [
        ("lookup",   "Test Lookup",                "/lookup"),
        ("projects", "Project Design & Execution", "/projects"),
    ]
    nav = "".join(
        f'<li class="nav-item"><a class="nav-link {"active" if k==active else ""}" href="{href}">{label}</a></li>'
        for k, label, href in top_nav_links
    )
    # Project sub-nav (only rendered when inside a project)
    if project:
        pid = project["id"]
        pname = project["name"]
        sub_links = [
            ("overview",    "Overview",  f"/projects/{pid}"),
            ("sample-size", "Planner",   f"/projects/{pid}/sample-size"),
            ("report",      "Reporting", f"/projects/{pid}/report"),
            ("csam",        "CSAM Gallery", f"/projects/{pid}/csam"),
            ("tracker",     "Schedule",  f"/projects/{pid}/tracker"),
        ]
        sub_nav_items = "".join(
            f'<li class="nav-item" style="margin-right:.25rem">'
            f'<a class="nav-link px-3 py-1" href="{href}" '
            f'style="font-size:.8rem;font-weight:{"600" if k==active_sub else "400"};'
            f'color:{"var(--df-accent)" if k==active_sub else "var(--df-mid)"};'
            f'border-bottom:{"2px solid var(--df-accent)" if k==active_sub else "2px solid transparent"};'
            f'border-radius:0;white-space:nowrap">{label}</a></li>'
            for k, label, href in sub_links
        )
        project_subnav = f"""
<div style="background:var(--df-white);border-bottom:1px solid var(--df-border);padding:0">
  <div class="container-xl d-flex align-items-center" style="min-height:44px;gap:.5rem">
    <a href="/projects" class="text-decoration-none"
       style="font-size:.78rem;color:var(--df-grey);white-space:nowrap;padding-right:.75rem;
              border-right:1px solid var(--df-border)">
      <i class="bi bi-arrow-left me-1"></i>Projects
    </a>
    <span style="font-size:.82rem;font-weight:600;color:var(--df-charcoal);
                 white-space:nowrap;padding-right:.75rem;border-right:1px solid var(--df-border)"
          title="{pname}">{pname[:40]}{"…" if len(pname)>40 else ""}</span>
    <ul class="navbar-nav flex-row mb-0" style="gap:.1rem">{sub_nav_items}</ul>
  </div>
</div>"""
    else:
        project_subnav = ""
    pt_label = "TTV" if part_type == "ttv" else "Active"
    pt_full  = "Thermal Test Vehicle" if part_type == "ttv" else "Active Device"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} — Package Reliability</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.1/font/bootstrap-icons.css" rel="stylesheet">
  <style>
    /* ── DF Brand Tokens ───────────────────────────── */
    :root {{
      --df-black:   #111111;
      --df-charcoal:#1c1c1c;
      --df-mid:     #555555;
      --df-grey:    #888888;
      --df-border:  #e0e0e0;
      --df-bg:      #f8f8f8;
      --df-white:   #ffffff;
      --df-accent:  #c8432a;   /* terracotta — from DF "READ MORE" links */
      --df-accent-h:#a83523;   /* hover */
      --df-pass:    #2d7a4f;
      --df-fail:    #c8432a;
    }}

    /* ── Base ───────────────────────────────────────── */
    body {{
      background: var(--df-white);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
      color: var(--df-black);
      font-size: .9rem;
    }}

    /* ── Navbar ─────────────────────────────────────── */
    .navbar {{
      background: var(--df-white) !important;
      border-bottom: 1px solid var(--df-border);
      padding-top: .9rem;
      padding-bottom: .9rem;
    }}
    .navbar-brand {{
      font-size: .9rem;
      font-weight: 500;
      color: var(--df-black) !important;
      display: flex;
      align-items: center;
      gap: .4rem;
    }}
    .navbar .nav-link {{
      color: var(--df-mid) !important;
      font-size: .875rem;
      font-weight: 400;
      padding: .5rem .9rem;
      border-bottom: 2px solid transparent;
    }}
    .navbar .nav-link:hover {{ color: var(--df-accent) !important; }}
    .navbar .nav-link.active {{
      color: var(--df-accent) !important;
      border-bottom: 2px solid var(--df-accent);
    }}
    .pt-pill {{
      font-size: .8rem;
      color: var(--df-grey);
      border: 1px solid var(--df-border);
      padding: .25rem .7rem;
      text-decoration: none;
      display: flex;
      align-items: center;
      gap: .4rem;
      transition: border-color .15s;
    }}
    .pt-pill:hover {{ border-color: var(--df-accent); color: var(--df-accent); }}
    .pt-pill .pt-dot {{
      width: 7px; height: 7px;
      border-radius: 50%;
      display: inline-block;
    }}

    /* ── Cards ──────────────────────────────────────── */
    .card {{
      border: 1px solid var(--df-border) !important;
      border-radius: 0 !important;
      box-shadow: none !important;
    }}
    .card-df {{
      background: var(--df-bg);
      color: var(--df-charcoal);
      padding: .65rem 1.25rem;
      border-radius: 0;
      font-size: .82rem;
      font-weight: 600;
      border-bottom: 1px solid var(--df-border);
    }}
    .card-df h5, .card-df h6 {{ font-size: .82rem; font-weight: 600; margin: 0; }}

    /* ── Buttons ────────────────────────────────────── */
    .btn {{
      border-radius: 0 !important;
      font-size: .85rem;
      font-weight: 400;
    }}
    .btn-primary {{
      background: var(--df-accent) !important;
      border-color: var(--df-accent) !important;
      color: #fff !important;
    }}
    .btn-primary:hover {{
      background: var(--df-accent-h) !important;
      border-color: var(--df-accent-h) !important;
    }}
    .btn-danger {{
      background: var(--df-charcoal) !important;
      border-color: var(--df-charcoal) !important;
      color: #fff !important;
    }}
    .btn-danger:hover {{ background: #333 !important; border-color: #333 !important; }}
    .btn-outline-secondary {{
      border-color: var(--df-border) !important;
      color: var(--df-black) !important;
    }}
    .btn-outline-secondary:hover {{ background: var(--df-bg) !important; }}
    .btn-outline-primary {{
      border-color: var(--df-black) !important;
      color: var(--df-black) !important;
      background: transparent !important;
    }}
    .btn-outline-primary:hover {{ background: var(--df-black) !important; color: #fff !important; }}
    .btn-lg {{ font-size: .78rem !important; padding: .75rem 2rem !important; }}

    /* ── Form controls ──────────────────────────────── */
    .form-control, .form-select {{
      border-radius: 0 !important;
      border-color: var(--df-border) !important;
      font-size: .88rem;
    }}
    .form-control:focus, .form-select:focus {{
      border-color: var(--df-black) !important;
      box-shadow: none !important;
    }}
    .form-label {{ font-size: .82rem; color: var(--df-mid); margin-bottom: .3rem; }}
    .form-control.is-invalid {{ border-color: var(--df-accent) !important; }}
    .form-control.is-invalid:focus {{ box-shadow: none !important; }}

    /* ── Tables ─────────────────────────────────────── */
    .tbl-header th {{
      background: var(--df-bg) !important;
      color: var(--df-mid) !important;
      font-size: .8rem !important;
      font-weight: 600 !important;
      border-bottom: 1px solid var(--df-border) !important;
    }}
    .table {{ border-color: var(--df-border); }}
    .table-sm td, .table-sm th {{ padding: .55rem .75rem; }}

    /* ── Badges ─────────────────────────────────────── */
    .badge {{ border-radius: 2px !important; font-weight: 400; font-size: .75rem; }}
    .bg-success {{ background: #d4edda !important; color: #1a5c35 !important; }}
    .bg-danger  {{ background: #f8d7da !important; color: #842029 !important; }}
    .bg-warning {{ background: #fff3cd !important; color: #664d03 !important; }}
    .bg-secondary {{ background: var(--df-bg) !important; color: var(--df-grey) !important; border: 1px solid var(--df-border); }}
    .bg-light   {{ background: var(--df-bg) !important; color: var(--df-mid) !important; border: 1px solid var(--df-border); }}

    /* ── Precond bar ────────────────────────────────── */
    .precond-bar {{
      background: #fdf6f4;
      border-left: 3px solid var(--df-accent);
      padding: .75rem 1rem;
      margin-bottom: 1.5rem;
      font-size: .85rem;
      color: var(--df-mid);
    }}

    /* ── Stat numbers ───────────────────────────────── */
    .stat-num {{ font-size: 2.4rem; font-weight: 300; line-height: 1.1; }}
    .result-pass {{ color: var(--df-pass); font-size: 1.5rem; font-weight: 300; letter-spacing: -.01em; }}
    .result-fail {{ color: var(--df-fail); font-size: 1.5rem; font-weight: 300; letter-spacing: -.01em; }}

    /* ── Report wrapper ─────────────────────────────── */
    .report-wrap {{ background: var(--df-white); border: 1px solid var(--df-border); padding: 2.5rem 3rem; }}
    .rpt-header {{
      background: var(--df-charcoal);
      color: #fff;
      padding: 1.4rem 2rem;
      margin-bottom: 1.5rem;
    }}
    .rpt-header .rpt-title {{
      font-size: .72rem;
      font-weight: 400;
      letter-spacing: .18em;
      text-transform: uppercase;
    }}
    .rpt-header .rpt-sub {{
      font-size: .75rem;
      color: rgba(255,255,255,.5);
      margin-top: .25rem;
      letter-spacing: .06em;
    }}

    /* ── Misc ───────────────────────────────────────── */
    h4, h5 {{ font-weight: 500; }}
    h6 {{ font-weight: 600; }}
    .text-muted {{ color: var(--df-grey) !important; }}
    hr {{ border-color: var(--df-border); }}
    .alert {{ border-radius: 0 !important; }}
    .alert-warning {{ background: #fdf6f4; border-color: var(--df-accent); color: var(--df-mid); }}
    .accordion-button {{ border-radius: 0 !important; font-size: .88rem; }}
    .accordion-item {{ border-radius: 0 !important; border-color: var(--df-border); }}
    .accordion-button:not(.collapsed) {{ background: var(--df-bg); color: var(--df-black); box-shadow: none; }}
    .accordion-button:focus {{ box-shadow: none; }}

    @media print {{
      .no-print {{ display: none !important; }}
      body {{ background: #fff; }}
      .report-wrap {{ border: none; padding: 0; }}
    }}
  </style>
</head>
<body>
<nav class="navbar navbar-expand-lg">
  <div class="container-xl">
    <a class="navbar-brand" href="/">
      Package Reliability
    </a>
    <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#nb"
            style="border:1px solid var(--df-border)">
      <span class="navbar-toggler-icon"></span>
    </button>
    <div class="collapse navbar-collapse" id="nb">
      <ul class="navbar-nav me-auto mb-0">{nav}</ul>
      <a href="/part-type" class="pt-pill text-decoration-none">
        <span class="pt-dot" style="background:{"#c8432a" if part_type=="ttv" else "#2d7a4f"}"></span>
        {pt_label} &mdash; {pt_full}
        <i class="bi bi-pencil-square ms-1" style="font-size:.65rem"></i>
      </a>
    </div>
  </div>
</nav>
{project_subnav}
<div class="container-xl py-5">
{body}
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
</body></html>"""


# ── Base Handler ───────────────────────────────────────────────────────────────

class Base(tornado.web.RequestHandler):
    def sess(self):
        return _get_or_create(self)

    def emit(self, body: str, title: str = "Package Reliability", active: str = "",
             project: dict = None, active_sub: str = ""):
        _, s = self.sess()
        pt = s.get("part_type", "ttv")
        self.finish(_page(active, pt, body, title, project=project, active_sub=active_sub))


# ── / → redirect ──────────────────────────────────────────────────────────────

class IndexHandler(Base):
    def get(self):
        self.redirect("/lookup")


# ── /part-type ────────────────────────────────────────────────────────────────

class PartTypeHandler(Base):
    def get(self):
        _, s = self.sess()
        pt = s.get("part_type", "ttv")
        nxt = self.get_argument("next", "/lookup")

        def sel(val):
            return "checked" if pt == val else ""
        def sel_style(val):
            if pt == val:
                return "border-left: 3px solid var(--df-accent); background: #fdf6f4;"
            return "border-left: 3px solid transparent;"

        body = f"""
        <div class="row justify-content-center">
          <div class="col-md-6 col-lg-5">
            <div class="card">
              <div class="card-df">Sample Part Type</div>
              <div class="card-body p-4">
                <p class="mb-4" style="font-size:.85rem;color:var(--df-grey)">Select the sample type for this session. Controls which qualification tests are shown throughout the app.</p>
                <form method="post">
                  <input type="hidden" name="next" value="{nxt}">
                  <div class="p-3 border mb-3" style="{sel_style('active')}">
                    <div class="form-check">
                      <input class="form-check-input" type="radio" name="part_type" id="pt_a" value="active" {sel('active')}>
                      <label class="form-check-label ms-1" for="pt_a">
                        <div style="font-size:.88rem;font-weight:500">Active Device</div>
                        <div style="font-size:.8rem;color:var(--df-grey);margin-top:.25rem">Full qualification suite — all JEDEC tests, including HTOL, ELFR, THB, ESD CDM/HBM, Latch-Up</div>
                      </label>
                    </div>
                  </div>
                  <div class="p-3 border mb-4" style="{sel_style('ttv')}">
                    <div class="form-check">
                      <input class="form-check-input" type="radio" name="part_type" id="pt_t" value="ttv" {sel('ttv')}>
                      <label class="form-check-label ms-1" for="pt_t">
                        <div style="font-size:.88rem;font-weight:500">Thermal Test Vehicle (TTV) — Inactive</div>
                        <div style="font-size:.8rem;color:var(--df-grey);margin-top:.25rem">Mechanical &amp; thermal tests only: uHAST, TC, T-Shock, M-Shock, Vibration, Pwr Cycling, HTS, Shadow Moiré</div>
                      </label>
                    </div>
                  </div>
                  <button type="submit" class="btn btn-primary w-100 py-2">
                    Set Part Type &amp; Continue &rarr;
                  </button>
                </form>
              </div>
            </div>
          </div>
        </div>"""
        self.emit(body, "Part Type")

    def post(self):
        _, s = self.sess()
        pt = self.get_argument("part_type", "active")
        if pt in ("active", "ttv"):
            s["part_type"] = pt
        self.redirect(self.get_argument("next", "/lookup"))


# ── /lookup ───────────────────────────────────────────────────────────────────

class LookupHandler(Base):
    def get(self):
        _, s = self.sess()
        tests = applicable_tests(s.get("part_type", "ttv"))

        import json as _json
        tc_js_data = _json.dumps(
            {ltr: {"tmin": v["tmin"], "tmax": v["tmax"],
                   "cycles": v["cycles"],
                   "soak": v["soak"],
                   "soakTimes": {str(m): TC_SOAK_TIMES[m] for m in v["soak"]},
                   "t4": v["t4"]}
             for ltr, v in TC_CONDITIONS.items()}
        )
        tshock_js_data = _json.dumps(TSHOCK_CONDITIONS)
        uhast_js_data   = _json.dumps(UHAST_CONDITIONS)
        mshock_js_data  = _json.dumps(MSHOCK_CONDITIONS)
        vib_sin_js_data = _json.dumps(VIB_SIN_CONDITIONS)
        vib_ran_js_data = _json.dumps(VIB_RAN_CONDITIONS)
        pc_js_data      = _json.dumps(PC_CONDITIONS)
        pc_method_js    = _json.dumps([{"label": m[0], "desc": m[1]} for m in PC_METHODS])
        hts_js_data     = _json.dumps(HTS_CONDITIONS)

        rows = ""
        for key, t in tests.items():
            badges = ""
            if t["active_devices"]:
                badges += '<span class="badge bg-info text-dark ms-2" style="font-size:.7rem">Active Device</span>'
            if t["destructive"]:
                badges += '<span class="badge bg-danger ms-2" style="font-size:.7rem">Destructive</span>'
            notes_row = f'<tr><th class="fw-normal text-muted pe-3">Notes</th><td>{t["notes"]}</td></tr>' if t["notes"] else ""
            spec_url  = SPEC_URLS.get(key, "")
            spec_link = (f'<a href="{spec_url}" target="_blank" rel="noopener" '
                         f'class="text-muted small ms-2" title="View JEDEC spec">'
                         f'<i class="bi bi-file-earmark-text"></i> spec</a>') if spec_url else ""
            std_link  = (f'<a href="{spec_url}" target="_blank" rel="noopener" '
                         f'class="text-decoration-none">{t["standard"]}</a>') if spec_url else t["standard"]

            # TC and T-Shock get interactive condition pickers; all others use static text
            if key == "tc":
                cond_opts = "".join(
                    f'<option value="{ltr}"{"  selected" if ltr == "H" else ""}>'
                    f'Condition {ltr} &nbsp;({v["tmin"]:+d}°C / {v["tmax"]:+d}°C)'
                    f'</option>'
                    for ltr, v in TC_CONDITIONS.items()
                )
                condition_cell = f"""<td>
                  <select id="tc-cond-sel" class="form-select form-select-sm mb-2"
                          style="max-width:260px" onchange="updateTcCond(this.value)">
                    {cond_opts}
                  </select>
                  <div style="font-size:.82rem;line-height:1.7">
                    <span id="tc-range" class="fw-semibold">+−55°C to +150°C</span>
                    &nbsp;<span class="text-muted">(Condition <span id="tc-ltr">H</span>)</span><br>
                    <span class="text-muted">Soak modes:</span> <span id="tc-soak">1 &amp; 2</span>
                    &ensp;|&ensp;
                    <span class="text-muted">Min soak time:</span> <span id="tc-soaktime">1 or 5 min</span><br>
                    <span id="tc-cycles-wrap">
                      <span class="text-muted">Cycles/hr (Table 3):</span> <span id="tc-cycles">2</span>
                    </span>
                    <span id="tc-t4-wrap" style="display:none">
                      <span class="text-muted">Cycles/hr (Table 4 — solder interconnect):</span>
                      <span id="tc-t4-detail"></span>
                    </span>
                  </div>
                </td>"""
            elif key == "tshock":
                ts_opts = "".join(
                    f'<option value="{ltr}"{"  selected" if ltr == "C" else ""}>'
                    f'Condition {ltr} &nbsp;({v["cold"]:+d}°C / {v["hot"]:+d}°C)'
                    f'</option>'
                    for ltr, v in TSHOCK_CONDITIONS.items()
                )
                condition_cell = f"""<td>
                  <select id="ts-cond-sel" class="form-select form-select-sm mb-2"
                          style="max-width:260px" onchange="updateTsCond(this.value)">
                    {ts_opts}
                  </select>
                  <div style="font-size:.82rem;line-height:1.7">
                    <span id="ts-range" class="fw-semibold">−55°C to +125°C</span>
                    &nbsp;<span class="text-muted">(Condition <span id="ts-ltr">C</span>)</span><br>
                    <span class="text-muted">Step 1 (hot):</span> <span id="ts-hot">+125°C</span>
                    &ensp;|&ensp;
                    <span class="text-muted">Step 2 (cold):</span> <span id="ts-cold">−55°C</span><br>
                    <span class="text-muted">Fluid S1:</span> <span id="ts-fluid1">Perfluorocarbon</span>
                    &ensp;|&ensp;
                    <span class="text-muted">Fluid S2:</span> <span id="ts-fluid2">Perfluorocarbon</span>
                  </div>
                </td>"""
            elif key == "vib":
                sin_opts = "".join(
                    f'<option value="{n}"{"  selected" if n == "1" else ""}>'
                    f'Condition {n} &nbsp;({v["accel_g"]}g / {v["fmin_hz"]}–{v["fmax_hz"]} Hz)'
                    f'</option>'
                    for n, v in VIB_SIN_CONDITIONS.items()
                )
                ran_opts = "".join(
                    f'<option value="{ltr}"{"  selected" if ltr == "A" else ""}>'
                    f'Condition {ltr} &nbsp;({v["rms_g"]}g RMS)'
                    f'</option>'
                    for ltr, v in VIB_RAN_CONDITIONS.items()
                )
                condition_cell = f"""<td>
                  <div class="btn-group btn-group-sm mb-2" role="group">
                    <input type="radio" class="btn-check" name="vib-type" id="vib-sin-radio" value="sin" checked
                           onchange="switchVibType('sin')">
                    <label class="btn btn-outline-secondary" for="vib-sin-radio">Sinusoidal</label>
                    <input type="radio" class="btn-check" name="vib-type" id="vib-ran-radio" value="ran"
                           onchange="switchVibType('ran')">
                    <label class="btn btn-outline-secondary" for="vib-ran-radio">Random</label>
                  </div>

                  <div id="vib-sin-panel">
                    <select id="vib-sin-sel" class="form-select form-select-sm mb-2"
                            style="max-width:310px" onchange="updateVibSin(this.value)">
                      {sin_opts}
                    </select>
                    <div style="font-size:.82rem;line-height:1.7">
                      <span id="vs-label" class="fw-semibold">20g peak, 20–2000 Hz</span>
                      &nbsp;<span class="text-muted">(Condition <span id="vs-num">1</span>)</span><br>
                      <span class="text-muted">Peak acceleration:</span> <span id="vs-accel">20 g</span>
                      &ensp;|&ensp;
                      <span class="text-muted">Frequency range:</span> <span id="vs-freq">20–2000 Hz</span><br>
                      <span class="text-muted">Displacement pk-pk:</span> <span id="vs-disp">0.060 in / 1.5 mm</span>
                      &ensp;|&ensp;
                      <span class="text-muted">Crossover:</span> <span id="vs-xover">80 Hz</span>
                    </div>
                  </div>

                  <div id="vib-ran-panel" style="display:none">
                    <select id="vib-ran-sel" class="form-select form-select-sm mb-2"
                            style="max-width:310px" onchange="updateVibRan(this.value)">
                      {ran_opts}
                    </select>
                    <div style="font-size:.82rem;line-height:1.7">
                      <span id="vr-label" class="fw-semibold">6.27g RMS</span>
                      &nbsp;<span class="text-muted">(Condition <span id="vr-ltr">A</span>)</span><br>
                      <span class="text-muted">RMS acceleration:</span> <span id="vr-accel">6.27 g</span>
                      &ensp;|&ensp;
                      <span class="text-muted">RMS velocity:</span> <span id="vr-vel">29.0 in/sec</span><br>
                      <span class="text-muted">RMS displacement:</span> <span id="vr-disp">0.926 in</span>
                      &ensp;|&ensp;
                      <span class="text-muted">6&times;RMS (3&sigma; pk-pk):</span> <span id="vr-sigma">5.55 in</span>
                    </div>
                  </div>
                </td>"""
            elif key == "mshock":
                ms_opts = "".join(
                    f'<option value="{ltr}"{"  selected" if ltr == "B" else ""}>'
                    f'Service Condition {ltr} &nbsp;({v["accel_g"]}g / {v["pulse_ms"]} ms)'
                    f'</option>'
                    for ltr, v in MSHOCK_CONDITIONS.items()
                )
                condition_cell = f"""<td>
                  <select id="ms-cond-sel" class="form-select form-select-sm mb-2"
                          style="max-width:310px" onchange="updateMsCond(this.value)">
                    {ms_opts}
                  </select>
                  <div style="font-size:.82rem;line-height:1.7">
                    <span id="ms-label" class="fw-semibold">1500g peak, 0.5 ms half-sine</span>
                    &nbsp;<span class="text-muted">(Service Condition <span id="ms-ltr">B</span>)</span><br>
                    <span class="text-muted">Acceleration peak:</span> <span id="ms-accel">1500 g</span>
                    &ensp;|&ensp;
                    <span class="text-muted">Pulse duration:</span> <span id="ms-pulse">0.5 ms</span><br>
                    <span class="text-muted">Velocity change:</span> <span id="ms-vel">468 cm/s &nbsp;(184 in/s)</span><br>
                    <span class="text-muted">Equiv. drop height:</span> <span id="ms-drop">112 cm &nbsp;(44 in)</span>
                  </div>
                </td>"""
            elif key == "uhast":
                uh_opts = "".join(
                    f'<option value="{ltr}"{"  selected" if ltr == "A" else ""}>'
                    f'Condition {ltr} &nbsp;({v["temp_db"]}°C / {v["rh"]}% RH)'
                    f'</option>'
                    for ltr, v in UHAST_CONDITIONS.items()
                )
                condition_cell = f"""<td>
                  <select id="uh-cond-sel" class="form-select form-select-sm mb-2"
                          style="max-width:280px" onchange="updateUhCond(this.value)">
                    {uh_opts}
                  </select>
                  <div style="font-size:.82rem;line-height:1.7">
                    <span id="uh-label" class="fw-semibold">130°C / 85% RH</span>
                    &nbsp;<span class="text-muted">(Condition <span id="uh-ltr">A</span>)</span><br>
                    <span class="text-muted">Temp (dry-bulb):</span> <span id="uh-tdb">130 ± 2°C</span>
                    &ensp;|&ensp;
                    <span class="text-muted">RH:</span> <span id="uh-rh">85 ± 5%</span><br>
                    <span class="text-muted">Temp (wet-bulb):</span> <span id="uh-twb">124.7°C</span>
                    &ensp;|&ensp;
                    <span class="text-muted">Vapor pressure:</span> <span id="uh-vp">230 kPa (33.3 psia)</span><br>
                    <span class="text-muted">Duration:</span> <span id="uh-dur">96 hours (−0, +2)</span>
                  </div>
                </td>"""
            elif key == "hts":
                hts_opts = "".join(
                    f'<option value="{ltr}"{"  selected" if ltr == "B" else ""}>'
                    f'Condition {ltr} &nbsp;(+{v["temp_c"]}°C)'
                    f'</option>'
                    for ltr, v in HTS_CONDITIONS.items()
                )
                condition_cell = f"""<td>
                  <select id="hts-cond-sel" class="form-select form-select-sm mb-2"
                          style="max-width:240px" onchange="updateHtsCond(this.value)">
                    {hts_opts}
                  </select>
                  <div style="font-size:.82rem;line-height:1.7">
                    <span id="hts-label" class="fw-semibold">+150°C storage</span>
                    &nbsp;<span class="text-muted">(Condition <span id="hts-ltr">B</span>)</span><br>
                    <span class="text-muted">Temperature:</span>
                    <span id="hts-temp">+150°C (−0 / +10°C)</span><br>
                    <span class="text-muted">Duration (JESD47 default):</span>
                    <span>1000 hours</span>
                    &ensp;<span class="text-muted small">(other durations acceptable per product requirements)</span>
                  </div>
                </td>"""
            elif key == "pc":
                pc_cond_opts = "".join(
                    f'<option value="{ltr}"{"  selected" if ltr == "B" else ""}>'
                    f'Condition {ltr} &nbsp;(Tmin {v["tmin"]}°C / Tmax {v["tmax"]}°C, ΔT {v["delta_t"]}°C)'
                    f'</option>'
                    for ltr, v in PC_CONDITIONS.items()
                )
                pc_method_opts = "".join(
                    f'<option value="{i}">{m[0]}</option>'
                    for i, m in enumerate(PC_METHODS)
                )
                condition_cell = f"""<td>
                  <div class="row g-2 mb-2">
                    <div class="col-auto">
                      <select id="pc-cond-sel" class="form-select form-select-sm"
                              style="max-width:340px" onchange="updatePcCond(this.value)">
                        {pc_cond_opts}
                      </select>
                    </div>
                  </div>
                  <div style="font-size:.82rem;line-height:1.7">
                    <span id="pc-label" class="fw-semibold">Condition B — ΔT 100°C</span><br>
                    <span class="text-muted">Tcycle(min):</span> <span id="pc-tmin">25°C (+5, −5)</span>
                    &ensp;|&ensp;
                    <span class="text-muted">Tcycle(max):</span> <span id="pc-tmax">+125°C (+5, −5)</span>
                    &ensp;|&ensp;
                    <span class="text-muted">ΔT:</span> <span id="pc-dt">100°C</span><br>
                    <span class="text-muted">Cycle rate:</span> <span>2–6 cycles/hr (typical)</span>
                  </div>
                  <div class="mt-2">
                    <label class="text-muted" style="font-size:.8rem">Test Method</label>
                    <select id="pc-method-sel" class="form-select form-select-sm mt-1"
                            style="max-width:340px" onchange="updatePcMethod(this.value)">
                      {pc_method_opts}
                    </select>
                    <div id="pc-method-desc" class="text-muted mt-1"
                         style="font-size:.8rem;font-style:italic">
                      Best for functional devices &amp; tightly controlled heater resistance; allows Tj variation modeling
                    </div>
                  </div>
                </td>"""
            else:
                condition_cell = f"<td>{t['condition']}</td>"

            rows += f"""
            <div class="accordion-item">
              <h2 class="accordion-header">
                <button class="accordion-button collapsed py-3" type="button"
                        data-bs-toggle="collapse" data-bs-target="#a_{key}" aria-expanded="false">
                  <strong class="me-2">{t['name']}</strong>
                  <span class="text-muted" style="font-size:.83rem">{t['standard']}</span>
                  {badges}
                  {spec_link}
                </button>
              </h2>
              <div id="a_{key}" class="accordion-collapse collapse">
                <div class="accordion-body pt-2 pb-3">
                  <div class="text-muted small mb-3" style="font-style:italic">{t['full_name']}</div>
                  <div class="row">
                    <div class="col-md-6">
                      <table class="table table-sm table-borderless mb-0">
                        <tr><th class="fw-normal text-muted pe-3" style="width:130px;white-space:nowrap">Precursor</th>
                            <td>PC ({PRECOND['full_name']}; {PRECOND['standard']})</td></tr>
                        <tr><th class="fw-normal text-muted pe-3">Standard</th><td>{std_link}</td></tr>
                        <tr><th class="fw-normal text-muted pe-3">Condition</th>{condition_cell}</tr>
                        <tr><th class="fw-normal text-muted pe-3">Duration</th><td>{t['duration']}</td></tr>
                        <tr><th class="fw-normal text-muted pe-3">Destructive</th><td>{'Yes' if t['destructive'] else 'No'}</td></tr>
                      </table>
                    </div>
                    <div class="col-md-6">
                      <table class="table table-sm table-borderless mb-0">
                        <tr><th class="fw-normal text-muted pe-3" style="width:130px;white-space:nowrap">Pre-test</th><td>{t['pre_testing']}</td></tr>
                        <tr><th class="fw-normal text-muted pe-3">Post-test</th><td>{t['post_testing']}</td></tr>
                        <tr><th class="fw-normal text-muted pe-3">Pass Criteria</th><td>{t['pass_criteria']}</td></tr>
                        {notes_row}
                      </table>
                    </div>
                  </div>
                </div>
              </div>
            </div>"""

        body = f"""
        <div class="d-flex align-items-center mb-3">
          <h4 class="mb-0" style="font-weight:300">Test Condition Lookup</h4>
        </div>
        <div class="precond-bar">
          <strong>Precursor for all tests &mdash;</strong>
          PC ({PRECOND['full_name']}; {PRECOND['standard']}) &nbsp;&middot;&nbsp;
          {PRECOND['condition']} &nbsp;&middot;&nbsp; {PRECOND['duration']}
          &nbsp;&middot;&nbsp; Pass: {PRECOND['pass_criteria']}
        </div>
        <div class="alert alert-info d-flex align-items-start gap-2 mb-3 mt-2 py-2 px-3" role="alert" style="font-size:.85rem">
          <i class="bi bi-info-circle-fill mt-1 flex-shrink-0"></i>
          <span>Test conditions shown reflect one recommended test case for thermal test vehicle.
          Other conditions may be selected from JEDEC standard based on device type, use environment, and applicable specifications.</span>
        </div>
        <div class="accordion shadow-sm" id="accTest">{rows}</div>

        <div class="card mt-4 mb-2 shadow-sm border-warning">
          <div class="card-header bg-warning bg-opacity-10 py-2 px-3 d-flex align-items-center gap-2">
            <i class="bi bi-exclamation-triangle-fill text-warning"></i>
            <strong style="font-size:.9rem">In Case of Part Failure</strong>
          </div>
          <div class="card-body px-3 py-2" style="font-size:.84rem; line-height:1.6">
            <p class="mb-2">The following guidance is drawn from <strong>JESD47I</strong>:</p>
            <ul class="mb-2 ps-3">
              <li><strong>Discounting failures (§3.6):</strong> A failure may be discounted from the sample count if it can be documented that the root cause is unrelated to the test conditions (e.g., handling damage, ESD, pre-existing defect). Written evidence of the unrelated cause is required.</li>
              <li><strong>Sample reusability (§3.5):</strong> Devices used in <em>nondestructive</em> tests may be reused in subsequent stress tests. Devices subjected to <em>destructive</em> analysis may not be reused for qualification — they are limited to engineering analysis only.</li>
              <li><strong>Failure analysis &amp; requalification (§4.2.3):</strong> Failed devices should be analyzed for root cause; only a <em>representative sample</em> needs to be analyzed, not every failed part. Successful requalification requires demonstrating corrective and preventive actions (CAPA). A part or qualification family may still be qualified provided containment of the problem is demonstrated while CAPAs are being implemented. Only the tests affected by the change that caused the failure need to be repeated — a full requalification from scratch is not required.</li>
            </ul>
            <p class="mb-0 text-muted" style="font-size:.8rem">Refer to JESD47I for full normative requirements. The same §3.8 sample size formula applies to any requalification run.</p>
          </div>
        </div>

        <div class="card mt-2 mb-2 shadow-sm border-info">
          <div class="card-header bg-info bg-opacity-10 py-2 px-3 d-flex align-items-center gap-2">
            <i class="bi bi-arrow-repeat text-info"></i>
            <strong style="font-size:.9rem">Reasons for Requalification</strong>
          </div>
          <div class="card-body px-3 py-2" style="font-size:.84rem; line-height:1.6">
            <p class="mb-2">Any of the following changes trigger a requalification requirement per JESD47I. Expand the applicable device category below.</p>

            <!-- TTV accordion -->
            <div class="accordion accordion-flush border rounded mb-2" id="requal-acc">
              <div class="accordion-item">
                <h2 class="accordion-header">
                  <button class="accordion-button collapsed py-2" type="button" data-bs-toggle="collapse" data-bs-target="#requal-ttv" style="font-size:.84rem; font-weight:600">
                    <i class="bi bi-cpu me-2 text-secondary"></i>Thermal Test Vehicle (TTV)
                  </button>
                </h2>
                <div id="requal-ttv" class="accordion-collapse collapse" data-bs-parent="#requal-acc">
                  <div class="accordion-body py-2 px-3">
                    <ol class="mb-0 ps-3" style="font-size:.83rem; line-height:1.7">
                      <li>Wafer Diameter Change</li>
                      <li><strong>Metallization:</strong> New materials or a significant change in composition</li>
                      <li><strong>Wafer Backside Operation:</strong> Metal composition, design rules, process and/or technique</li>
                      <li><strong>Die Coating:</strong> Material, process, and/or technique</li>
                      <li><strong>Bonding:</strong> Process and/or technique</li>
                      <li>Die Thickness</li>
                      <li><strong>Package Dimension Change:</strong> Larger package body size or reduction in lead or solder ball pitch</li>
                    </ol>
                  </div>
                </div>
              </div>

              <!-- Active device accordion -->
              <div class="accordion-item">
                <h2 class="accordion-header">
                  <button class="accordion-button collapsed py-2" type="button" data-bs-toggle="collapse" data-bs-target="#requal-active" style="font-size:.84rem; font-weight:600">
                    <i class="bi bi-diagram-3 me-2 text-secondary"></i>Active Device
                  </button>
                </h2>
                <div id="requal-active" class="accordion-collapse collapse" data-bs-parent="#requal-acc">
                  <div class="accordion-body py-2 px-3">
                    <ol class="mb-0 ps-3" style="font-size:.83rem; line-height:1.7">
                      <li><strong>Active Circuit Element:</strong> New type of circuit element or modification of transistors beyond original qualification or spec limits</li>
                      <li><strong>Major Circuit Elements:</strong> Addition of a major new circuit block to an existing circuit such as adding a Digital Signal Processor or embedded memory block to an existing product</li>
                      <li><strong>Wafer Diameter Change / Metallization:</strong> New materials or a significant change in composition</li>
                      <li><strong>Change In Minimum Feature Size:</strong> A reduction of greater than 20% shall be considered a new process</li>
                      <li><strong>Wafer Fab Process:</strong> Utilizing different process techniques at critical points (excluding wafer transport equipment)</li>
                      <li><strong>Diffusion/Dopant:</strong> New material or technique</li>
                      <li><strong>Polysilicon or other MOSFET gate material:</strong> Composition, design rules, process</li>
                      <li><strong>Lithography:</strong> Change in wavelength, method (air / immersion / e-beam), or etch technique</li>
                      <li><strong>Wafer Frontside Metallization:</strong> Composition, design rules, process and/or technique</li>
                      <li><strong>VIA:</strong> Composition, design rules, process and/or technique</li>
                      <li><strong>Passivation Overcoat:</strong> Either glass or organic material composition, design rules, process and/or technique</li>
                      <li><strong>Dielectric Materials:</strong> Composition, design rules, process and/or technique</li>
                      <li><strong>Low-K Dielectric:</strong> A dielectric material used for inter-metal isolation with a K value less than 3.2</li>
                      <li><strong>Wafer Backside Operation:</strong> Metal composition, design rules, process and/or technique</li>
                      <li><strong>New Wafer Manufacturing Line:</strong> Not already qualified for the fabrication process</li>
                      <li><strong>Assembly Process:</strong> Utilizing different process techniques at critical points</li>
                      <li><strong>Die Coating:</strong> Material, process, and/or technique</li>
                      <li><strong>Lead Frame:</strong> Base material, finish, and critical dimensions</li>
                      <li><strong>Bond Wire:</strong> Material, diameter</li>
                      <li><strong>Bonding / Die Preparation:</strong> Process and/or technique; separation and clean methods</li>
                      <li><strong>Die Attach:</strong> Material, process, and/or technique</li>
                      <li><strong>Encapsulation:</strong> Material, composition, process and/or technique</li>
                      <li><strong>Hermetic Package:</strong> Material, composition, seal material, process and/or technique</li>
                      <li><strong>Wafer Bumping Material:</strong> Process or technique (including flip chip assembly process)</li>
                      <li><strong>Package Dimension Change:</strong> Larger package body size or reduction in lead or solder ball pitch</li>
                      <li>Die Thickness</li>
                      <li>New Chip-Package Combination</li>
                    </ol>
                  </div>
                </div>
              </div>
            </div>

            <p class="mb-0 text-muted" style="font-size:.8rem">Source: JESD47I Table 1 &amp; §4.2. Only tests affected by the triggering change need to be repeated.</p>
          </div>
        </div>

        <div class="card mt-2 mb-2 shadow-sm">
          <div class="card-body py-2 px-3 d-flex align-items-center gap-3" style="font-size:.85rem">
            <i class="bi bi-file-earmark-pdf text-danger fs-5"></i>
            <div>
              <strong>JESD47I</strong> — Stress-Test-Driven Qualification of Integrated Circuits
              <span class="text-muted ms-2" style="font-size:.8rem">Generic qualification guidelines &amp; pass/fail criteria</span>
            </div>
            <a href="{SPEC_URLS['jesd47']}" target="_blank" class="btn btn-sm btn-outline-secondary ms-auto">
              <i class="bi bi-download me-1"></i>Download PDF
            </a>
          </div>
        </div>
        <script>
        const TC_DATA     = {tc_js_data};
        const TSHOCK_DATA = {tshock_js_data};
        const UHAST_DATA   = {uhast_js_data};
        const MSHOCK_DATA   = {mshock_js_data};
        const VIB_SIN_DATA  = {vib_sin_js_data};
        const VIB_RAN_DATA  = {vib_ran_js_data};
        const PC_DATA       = {pc_js_data};
        const PC_METHODS    = {pc_method_js};
        const HTS_DATA      = {hts_js_data};

        function fmtTemp(v) {{
          return (v >= 0 ? "+" : "\u2212") + Math.abs(v) + "\u00b0C";
        }}

        function updateTcCond(ltr) {{
          const c = TC_DATA[ltr];
          if (!c) return;
          document.getElementById("tc-ltr").textContent   = ltr;
          document.getElementById("tc-range").textContent = fmtTemp(c.tmin) + " to " + fmtTemp(c.tmax);

          // Soak modes label (e.g. "1, 2 & 3")
          const modes = c.soak;
          const modeStr = modes.length > 1
            ? modes.slice(0, -1).join(", ") + " & " + modes[modes.length - 1]
            : "" + modes[0];
          document.getElementById("tc-soak").textContent = modeStr;

          // Min soak times (e.g. "1, 5, or 10 min")
          const times = modes.map(m => c.soakTimes[String(m)]);
          const timeStr = times.length > 1
            ? times.slice(0, -1).join(", ") + " or " + times[times.length - 1] + " min"
            : times[0] + " min";
          document.getElementById("tc-soaktime").textContent = timeStr;

          // Cycles/hr — Table 4 if available, else Table 3
          const t4 = c.t4 || {{}};
          const t4Keys = Object.keys(t4);
          if (t4Keys.length > 0) {{
            // Show per-soak-mode rates from Table 4
            const detail = t4Keys.map(m => "Soak " + m + ": " + t4[m] + " cph").join(" \u2022 ");
            document.getElementById("tc-t4-detail").textContent = " " + detail;
            document.getElementById("tc-cycles-wrap").style.display = "none";
            document.getElementById("tc-t4-wrap").style.display    = "";
          }} else {{
            // Fall back to Table 3
            document.getElementById("tc-cycles").textContent        = c.cycles;
            document.getElementById("tc-cycles-wrap").style.display = "";
            document.getElementById("tc-t4-wrap").style.display     = "none";
          }}
        }}

        function updateTsCond(ltr) {{
          const c = TSHOCK_DATA[ltr];
          if (!c) return;
          document.getElementById("ts-ltr").textContent    = ltr;
          document.getElementById("ts-range").textContent  = fmtTemp(c.cold) + " to " + fmtTemp(c.hot);
          document.getElementById("ts-hot").textContent    = fmtTemp(c.hot);
          document.getElementById("ts-cold").textContent   = fmtTemp(c.cold);
          document.getElementById("ts-fluid1").textContent = c.fluid_s1;
          document.getElementById("ts-fluid2").textContent = c.fluid_s2;
        }}

        function updateHtsCond(ltr) {{
          const c = HTS_DATA[ltr];
          if (!c) return;
          document.getElementById("hts-ltr").textContent   = ltr;
          document.getElementById("hts-label").textContent = "+" + c.temp_c + "\u00b0C storage";
          document.getElementById("hts-temp").textContent  = "+" + c.temp_c + "\u00b0C (\u22120 / +10\u00b0C)";
        }}

        function updatePcCond(ltr) {{
          const c = PC_DATA[ltr];
          if (!c) return;
          document.getElementById("pc-label").textContent = "Condition " + ltr + " \u2014 \u0394T " + c.delta_t + "\u00b0C";
          document.getElementById("pc-tmin").textContent  = c.tmin + "\u00b0C (+5, \u22125)";
          document.getElementById("pc-tmax").textContent  = "+" + c.tmax + "\u00b0C (+5, \u22125)";
          document.getElementById("pc-dt").textContent    = c.delta_t + "\u00b0C";
        }}

        function updatePcMethod(idx) {{
          const m = PC_METHODS[parseInt(idx)];
          if (!m) return;
          document.getElementById("pc-method-desc").innerHTML = m.desc;
        }}

        function switchVibType(type) {{
          document.getElementById("vib-sin-panel").style.display = type === "sin" ? "" : "none";
          document.getElementById("vib-ran-panel").style.display = type === "ran" ? "" : "none";
        }}

        function updateVibSin(num) {{
          const c = VIB_SIN_DATA[num];
          if (!c) return;
          document.getElementById("vs-num").textContent   = num;
          document.getElementById("vs-label").textContent = c.accel_g + "g peak, " + c.fmin_hz + "\u2013" + c.fmax_hz + " Hz";
          document.getElementById("vs-accel").textContent = c.accel_g + " g";
          document.getElementById("vs-freq").textContent  = c.fmin_hz + "\u2013" + c.fmax_hz + " Hz";
          document.getElementById("vs-disp").textContent  = c.disp_in + " in / " + c.disp_mm + " mm";
          document.getElementById("vs-xover").textContent = c.xover_hz + " Hz";
        }}

        function updateVibRan(ltr) {{
          const c = VIB_RAN_DATA[ltr];
          if (!c) return;
          document.getElementById("vr-ltr").textContent   = ltr;
          document.getElementById("vr-label").textContent = c.rms_g + "g RMS";
          document.getElementById("vr-accel").textContent = c.rms_g + " g";
          document.getElementById("vr-vel").textContent   = c.vel_ins + " in/sec";
          document.getElementById("vr-disp").textContent  = c.disp_in + " in";
          document.getElementById("vr-sigma").textContent = c.sigma_in + " in";
        }}

        function updateMsCond(ltr) {{
          const c = MSHOCK_DATA[ltr];
          if (!c) return;
          document.getElementById("ms-ltr").textContent   = ltr;
          document.getElementById("ms-label").textContent = c.accel_g + "g peak, " + c.pulse_ms + " ms half-sine";
          document.getElementById("ms-accel").textContent = c.accel_g + " g";
          document.getElementById("ms-pulse").textContent = c.pulse_ms + " ms";
          document.getElementById("ms-vel").textContent   = c.vel_cms + " cm/s \u00a0(" + c.vel_ins + " in/s)";
          document.getElementById("ms-drop").textContent  = c.drop_cm + " cm \u00a0(" + c.drop_in + " in)";
        }}

        function updateUhCond(ltr) {{
          const c = UHAST_DATA[ltr];
          if (!c) return;
          document.getElementById("uh-ltr").textContent  = ltr;
          document.getElementById("uh-label").textContent = c.temp_db + "\u00b0C / " + c.rh + "% RH";
          document.getElementById("uh-tdb").textContent  = c.temp_db + " \u00b1 2\u00b0C";
          document.getElementById("uh-rh").textContent   = c.rh + " \u00b1 5%";
          document.getElementById("uh-twb").textContent  = c.temp_wb + "\u00b0C";
          document.getElementById("uh-vp").textContent   = c.vp_kpa + " kPa (" + c.vp_psia + " psia)";
          document.getElementById("uh-dur").textContent  = c.duration;
        }}
        </script>"""
        self.emit(body, "Test Lookup", "lookup")


# ── /sample-size ──────────────────────────────────────────────────────────────

class SampleSizeHandler(Base):
    def get(self):
        self._render()

    def post(self):
        try:
            k    = int(self.get_argument("failures", "0"))
            ltpd = float(self.get_argument("ltpd", "5"))
            if k < 0:              raise ValueError("Acceptance number C must be ≥ 0")
            if not (0.01 <= ltpd <= 99.99): raise ValueError("LTPD must be between 0.01% and 99.99%")
            n_jesd47 = min_sample_size_ltpd(ltpd, k)
            r_equiv  = 1.0 - ltpd / 100.0
            n_exact  = min_sample_size(r_equiv, 0.90, k)
            self._render(k=k, ltpd=ltpd, n_jesd47=n_jesd47, n_exact=n_exact)
        except Exception as e:
            self._render(error=str(e))

    def _render(self, k=0, ltpd=5, n_jesd47=None, n_exact=None, error=None):
        # Build Table A HTML — highlight nearest standard LTPD column and selected C row
        ltpd_cols = TABLE_A_LTPD   # [10, 7, 5, 3, 2, 1.5, 1]
        nearest_ltpd = min(ltpd_cols, key=lambda l: abs(l - ltpd)) if n_jesd47 is not None else None
        hdr_cells = "".join(
            f'<th class="{"table-primary fw-bold" if l == nearest_ltpd else ""}">'
            f'LTPD {l}%</th>'
            for l in ltpd_cols
        )
        tbl_rows = ""
        for c_val, row in TABLE_A.items():
            row_cls = 'class="table-primary"' if (n_jesd47 is not None and c_val == k) else ""
            cells = ""
            for ci, n_val in enumerate(row):
                is_sel = (n_jesd47 is not None and c_val == k and ltpd_cols[ci] == nearest_ltpd)
                td_cls = ' class="fw-bold text-primary"' if is_sel else ""
                cells += f"<td{td_cls}>{n_val}</td>"
            tbl_rows += f"<tr {row_cls}><td><strong>{c_val}</strong></td>{cells}</tr>"

        table_a_html = f"""
        <div class="card mt-4">
          <div class="card-df d-flex align-items-center">
            <h6 class="mb-0">Table A — JESD47I §3.8</h6>
            <span class="text-white-50 ms-2" style="font-size:.78rem">Sample Size for Maximum % Defective at 90% Confidence</span>
          </div>
          <div class="table-responsive">
            <table class="table table-sm table-bordered mb-0" style="font-size:.8rem">
              <thead class="tbl-header">
                <tr><th>Accept # (C)</th>{hdr_cells}</tr>
              </thead>
              <tbody>{tbl_rows}</tbody>
            </table>
          </div>
          <div class="card-footer text-muted py-1 px-3" style="font-size:.75rem">
            Highlighted cell = selected C &amp; LTPD.
            <a href="{SPEC_URLS['jesd47']}" target="_blank" class="ms-2">
              <i class="bi bi-file-earmark-pdf me-1"></i>JESD47I PDF
            </a>
          </div>
        </div>"""

        result_html = ""
        if error:
            result_html = f'<div class="alert alert-danger"><i class="bi bi-exclamation-triangle me-2"></i>{error}</div>'
        elif n_jesd47 is not None:
            r_equiv = 1.0 - ltpd / 100.0
            result_html = f"""
            <div class="card mb-3">
              <div class="card-body text-center py-4">
                <div class="text-muted small mb-1">Minimum Sample Size (JESD47I)</div>
                <div class="stat-num" style="color:var(--df-navy)">{n_jesd47}</div>
                <div class="text-muted mt-2 small">
                  to demonstrate ≤<strong>{ltpd}%</strong> defective at
                  <strong>90%</strong> confidence with <strong>{k}</strong> allowed failure(s)
                </div>
              </div>
            </div>
            <div class="card mb-2">
              <div class="card-body py-3 px-4" style="font-size:.82rem">
                <div class="mb-1"><strong>JESD47I §3.8 formula:</strong></div>
                <div class="text-muted font-monospace">N &ge; 0.5 &times; &chi;&sup2;(2C+2,&thinsp;0.1) &times; (1/LTPD &minus; 0.5) + C</div>
                <div class="mt-2 text-muted">
                  Exact chi-squared (R={r_equiv:.3f}, C=90%): <strong>{n_exact}</strong>
                  {"&ensp;<span class='badge bg-secondary'>same</span>" if n_exact == n_jesd47 else f"&ensp;<span class='text-muted'>({'+' if n_jesd47>n_exact else ''}{n_jesd47-n_exact} vs JESD47I)</span>"}
                </div>
              </div>
            </div>"""
        else:
            result_html = """<div class="card text-center p-5" style="color:var(--df-grey)">
              <p class="mb-0" style="font-size:.85rem">Select parameters and click Calculate &rarr;</p></div>"""

        body = f"""
        <h4 class="mb-4" style="font-weight:300">Sample Size Planner</h4>
        <div class="row g-4">
          <div class="col-md-4">
            <div class="card">
              <div class="card-df"><h6 class="mb-0">Parameters</h6></div>
              <div class="card-body p-4">
                <p class="text-muted small mb-3">
                  Per <strong>JESD47I §3.8</strong> — how many units are required to satisfy
                  a maximum defect level at 90% confidence?
                </p>
                <form method="post">
                  <div class="mb-3">
                    <label class="form-label">LTPD — Max % Defective</label>
                    <div class="input-group">
                      <input type="number" class="form-control" name="ltpd"
                             value="{ltpd}" min="0.01" max="99.99" step="0.01" required>
                      <span class="input-group-text">%</span>
                    </div>
                    <div class="form-text">Lot Tolerance Percent Defective (any value)</div>
                  </div>
                  <div class="mb-4">
                    <label class="form-label">Acceptance Number (C)</label>
                    <input type="number" class="form-control" name="failures"
                           value="{k}" min="0" step="1" required>
                    <div class="form-text">Max allowed failures (0 = zero-failure plan)</div>
                  </div>
                  <button type="submit" class="btn btn-primary w-100">Calculate &rarr;</button>
                </form>
                <hr class="my-3">
                <p class="text-muted mb-0" style="font-size:.78rem">
                  Confidence is fixed at 90% per JESD47I.<br>
                  LTPD = (1&minus;R) &times; 100, e.g. LTPD&nbsp;5% &equiv; R&nbsp;=&nbsp;95%.
                </p>
              </div>
            </div>
          </div>
          <div class="col-md-8">
            {result_html}
            {table_a_html}
          </div>
        </div>"""
        self.emit(body, "Sample Size Planner", "sample-size")


# ── /pass-fail ────────────────────────────────────────────────────────────────

class PassFailHandler(Base):
    def get(self):
        self._render()

    def post(self):
        try:
            n        = int(self.get_argument("n",          ""))
            k        = int(self.get_argument("failures",   "0"))
            c        = float(self.get_argument("confidence", "0.90"))
            ltpd_inp = float(self.get_argument("ltpd_pct", "5.0"))
            if n < 1:        raise ValueError("Need at least 1 sample")
            if k > n:        raise ValueError("Failures cannot exceed samples tested")
            if not (0.50 <= c <= 0.9999):            raise ValueError("Confidence must be 0.50–0.9999")
            if not (0.01 <= ltpd_inp <= 99.99):      raise ValueError("Defective Rate must be 0.01–99.99%")
            r_req = 1.0 - ltpd_inp / 100.0
            passed, r_demo = _pf(n, k, c, r_req)
            sens = [(f, demonstrated_reliability(n, f, c)) for f in range(min(n+1, 9))]
            self._render(n=n, k=k, c=c, r_req=r_req, ltpd_inp=ltpd_inp,
                         passed=passed, r_demo=r_demo, sens=sens)
        except (ValueError, TypeError) as e:
            self._render(error=str(e))

    def _render(self, n="", k=0, c=0.90, r_req=0.95, ltpd_inp=5.0,
                passed=None, r_demo=None, sens=None, error=None):
        result_html = ""
        if error:
            result_html = f'<div class="alert alert-danger"><i class="bi bi-exclamation-triangle me-2"></i>{error}</div>'
        elif passed is not None:
            verdict_cls  = "result-pass" if passed else "result-fail"
            verdict_icon = "check-circle-fill" if passed else "x-circle-fill"
            verdict_text = "PASS" if passed else "FAIL"
            defect_demo  = (1.0 - r_demo) * 100.0
            detail_text  = (f"Demonstrated defect rate {defect_demo:.2f}% ≤ LTPD {ltpd_inp:.2f}%" if passed
                            else f"Demonstrated defect rate {defect_demo:.2f}% exceeds LTPD {ltpd_inp:.2f}%")
            # JESD47I Table A minimum n for equivalent LTPD
            ltpd_pct   = ltpd_inp
            # Find closest standard LTPD column
            std_ltpd   = min(TABLE_A_LTPD, key=lambda l: abs(l - ltpd_pct))
            jesd47_n   = min_sample_size_ltpd(std_ltpd, k) if k <= 12 else None
            jesd47_note = ""
            if jesd47_n is not None:
                ltpd_ok = n >= jesd47_n
                ltpd_badge = "success" if ltpd_ok else "danger"
                jesd47_note = (f'<div class="text-muted small mt-2">'
                               f'JESD47I Table A (LTPD&nbsp;{std_ltpd}%, C={k}): '
                               f'min&nbsp;n&nbsp;=&nbsp;<strong>{jesd47_n}</strong> &ensp;'
                               f'<span class="badge bg-{ltpd_badge}">{"meets" if ltpd_ok else "below"} JESD47I</span>'
                               f'</div>')

            extra = ""
            if not passed:
                n_needed = min_sample_size(r_req, c, k)
                gap = n_needed - n
                if gap > 0:
                    extra = f'<div class="text-muted small mt-1">→ {gap} more samples needed (0 additional failures) to reach target</div>'

            def _pf_row(f, r_f, k=k, r_req=r_req):
                tr_cls   = 'class="table-primary"' if f == k else ""
                bg       = "bg-success" if r_f >= r_req else "bg-danger"
                verdict  = "PASS" if r_f >= r_req else "FAIL"
                actual   = '<span class="badge bg-primary ms-1">actual</span>' if f == k else ""
                return (f'<tr {tr_cls}><td>{f}</td><td>{r_f*100:.2f}%</td>'
                        f'<td><span class="badge {bg}">{verdict}</span>{actual}</td></tr>')
            sens_rows = "".join(_pf_row(f, r_f) for f, r_f in (sens or []))
            result_html = f"""
            <div class="card mb-3">
              <div class="card-body text-center py-4">
                <div class="{verdict_cls} mb-1"><i class="bi bi-{verdict_icon} me-2"></i>{verdict_text}</div>
                <div class="text-muted small">{detail_text}</div>
                {extra}
                {jesd47_note}
                <hr class="my-3">
                <div class="row g-3 text-center">
                  <div class="col">
                    <div class="text-muted" style="font-size:.75rem">DEFECT RATE (DEMO)</div>
                    <div class="fw-bold fs-5">{defect_demo:.2f}%</div>
                  </div>
                  <div class="col">
                    <div class="text-muted" style="font-size:.75rem">LTPD (MAX)</div>
                    <div class="fw-bold fs-5">{ltpd_inp:.2f}%</div>
                  </div>
                  <div class="col">
                    <div class="text-muted" style="font-size:.75rem">CONFIDENCE</div>
                    <div class="fw-bold fs-5">{c*100:.0f}%</div>
                  </div>
                  <div class="col">
                    <div class="text-muted" style="font-size:.75rem">n / failures</div>
                    <div class="fw-bold fs-5">{n} / {k}</div>
                  </div>
                </div>
              </div>
            </div>
            <div class="card">
              <div class="card-header bg-light py-2">
                <strong>Sensitivity Analysis</strong>
                <span class="text-muted small ms-2">same n={n}, same confidence — what if failures differed?</span>
              </div>
              <table class="table table-hover table-sm mb-0">
                <thead class="tbl-header"><tr><th>Failures</th><th>Demonstrated R</th><th>Result vs Target</th></tr></thead>
                <tbody>{sens_rows}</tbody>
              </table>
            </div>"""
        else:
            result_html = """<div class="card text-center p-5" style="color:var(--df-grey)">
              <p class="mb-0" style="font-size:.85rem;letter-spacing:.05em">Enter test results and click Evaluate &rarr;</p></div>"""

        body = f"""
        <h4 class="mb-4" style="font-weight:300">Pass / Fail Determination</h4>
        <div class="row g-4">
          <div class="col-md-5">
            <div class="card">
              <div class="card-df"><h6 class="mb-0">Test Results</h6></div>
              <div class="card-body p-4">
                <p class="text-muted small mb-3">Given actual results, what reliability was demonstrated?</p>
                <form method="post">
                  <div class="mb-3">
                    <label class="form-label">Samples Tested</label>
                    <input type="number" class="form-control" name="n" value="{n}" min="1" required placeholder="e.g. 77">
                  </div>
                  <div class="mb-3">
                    <label class="form-label">Failures Observed</label>
                    <input type="number" class="form-control" name="failures" value="{k}" min="0" required>
                  </div>
                  <div class="mb-3">
                    <label class="form-label">Confidence Level</label>
                    <div class="input-group">
                      <input type="number" class="form-control" name="confidence" value="{c}" step="0.001" min="0.50" max="0.9999" required>
                      <span class="input-group-text text-muted small">e.g. 0.90</span>
                    </div>
                  </div>
                  <div class="mb-4">
                    <label class="form-label">Defective Rate % (LTPD)</label>
                    <div class="input-group">
                      <input type="number" class="form-control" name="ltpd_pct" value="{ltpd_inp}" step="0.01" min="0.01" max="99.99" required>
                      <span class="input-group-text text-muted small">% defective</span>
                    </div>
                  </div>
                  <button type="submit" class="btn btn-primary w-100">
                    <i class="bi bi-check2-circle me-2"></i>Evaluate
                  </button>
                </form>
              </div>
            </div>
          </div>
          <div class="col-md-7">{result_html}</div>
        </div>"""
        self.emit(body, "Pass / Fail", "pass-fail")


# ── /report (GET = form, POST = generate) ─────────────────────────────────────

class ReportHandler(Base):
    def get(self):
        _, s = self.sess()
        pt = s.get("part_type", "ttv")
        tests = applicable_tests(pt)
        self._render_form(tests)

    def post(self):
        _, s = self.sess()
        pt    = s.get("part_type", "ttv")
        tests = applicable_tests(pt)

        customer = self.get_argument("customer", "Customer").strip() or "Customer"
        product  = self.get_argument("product",  "SCD-on-Si Package").strip()
        author   = self.get_argument("author",   "").strip()

        # Parse part description
        part_desc = {}
        if pt == "ttv":
            part_desc["si_thick"]      = self.get_argument("part_si_thick", "50").strip() or "50"
            part_desc["bond_type"]     = self.get_argument("part_bond_type", "Ag Sinter").strip() or "Ag Sinter"
            part_desc["diamond_thick"] = self.get_argument("part_diamond_thick", "300").strip() or "300"
        else:
            part_desc["part_number"] = self.get_argument("part_number", "").strip()
            part_desc["part_tech"]   = self.get_argument("part_tech", "").strip()
            part_desc["part_package"] = self.get_argument("part_package", "").strip()

        entries = []
        samples = {}
        for key, t in tests.items():
            sc    = self.get_argument(f"status_{key}", "ns")
            label = STATUS_LABEL.get(sc, "NOT STARTED")
            n_raw = self.get_argument(f"n_{key}", "").strip()
            k_raw = self.get_argument(f"k_{key}", "0").strip()
            notes = self.get_argument(f"notes_{key}", "").strip()

            try:    n_val = int(n_raw) if n_raw else None
            except: n_val = None
            try:    k_val = int(k_raw)
            except: k_val = 0

            is_char = t.get("characterization_only", False)
            char_result = self.get_argument(f"char_result_{key}", "").strip() if is_char else ""

            r_demo = stat_pass = None
            corrected = False
            if not is_char:
                # Compute reliability stats whenever test is in-progress or complete
                # Use project-specific LTPD and confidence if injected by ProjectReportHandler
                _conf = getattr(self, "_qual_confidence", 0.90)
                _ltpd = getattr(self, "_qual_ltpd", 5.0)
                _r_req = 1.0 - _ltpd / 100.0
                if sc in ("co", "ip") and n_val:
                    r_demo    = demonstrated_reliability(n_val, k_val, _conf)
                    stat_pass, _ = _pf(n_val, k_val, _conf, _r_req)

            entries.append({
                "key": key, "test": t, "sc": sc, "status": label,
                "n": n_val, "k": k_val, "notes": notes,
                "r_demo": r_demo, "stat_pass": stat_pass,
                "corrected": corrected,
                "is_char": is_char, "char_result": char_result,
            })

            # Parse sample records (TTV only)
            if pt == "ttv":
                samples_for_test = []
                n_samples_key = f"n_samples_{key}"
                n_samples_str = self.get_argument(n_samples_key, "0").strip() or "0"
                try:
                    n_samples = int(n_samples_str)
                except:
                    n_samples = 0

                for i in range(n_samples):
                    sid = self.get_argument(f"sid_{key}_{i}", f"SN-{i+1:03d}").strip() or f"SN-{i+1:03d}"

                    # CSAM values
                    csam_bpc_str = self.get_argument(f"csam_bpc_{key}_{i}", "").strip()
                    csam_apc_str = self.get_argument(f"csam_apc_{key}_{i}", "").strip()
                    csam_atst_str = self.get_argument(f"csam_atst_{key}_{i}", "").strip()

                    try:
                        csam_bpc = float(csam_bpc_str) if csam_bpc_str else None
                    except:
                        csam_bpc = None
                    try:
                        csam_apc = float(csam_apc_str) if csam_apc_str else None
                    except:
                        csam_apc = None
                    try:
                        csam_atst = float(csam_atst_str) if csam_atst_str else None
                    except:
                        csam_atst = None

                    csam_status, csam_badge = _csam_eval(csam_bpc, csam_apc, csam_atst)

                    # Thermal and Func
                    thermal = self.get_argument(f"thermal_{key}_{i}", "pass").strip() or "pass"
                    func = self.get_argument(f"func_{key}_{i}", "pass").strip() or "pass"

                    # Failed RTDs
                    failed_rtds_str = self.get_argument(f"failed_rtds_{key}_{i}", "").strip()
                    failed_rtds = [x.strip().upper() for x in failed_rtds_str.split(",") if x.strip()]

                    # File uploads (base64)
                    img_bpc = ""
                    img_apc = ""
                    img_atst = ""

                    for suffix, var_name in [("bpc", "img_bpc"), ("apc", "img_apc"), ("atst", "img_atst")]:
                        files = self.request.files.get(f"img_{suffix}_{key}_{i}", [])
                        if files:
                            f = files[0]
                            mime = f.get("content_type", "image/jpeg")
                            b64 = base64.b64encode(f["body"]).decode()
                            img_uri = f"data:{mime};base64,{b64}"
                            if var_name == "img_bpc":
                                img_bpc = img_uri
                            elif var_name == "img_apc":
                                img_apc = img_uri
                            elif var_name == "img_atst":
                                img_atst = img_uri

                    samples_for_test.append({
                        "id": sid,
                        "csam_bpc": csam_bpc,
                        "csam_apc": csam_apc,
                        "csam_atst": csam_atst,
                        "csam_status": csam_status,
                        "csam_badge": csam_badge,
                        "thermal": thermal,
                        "failed_rtds": failed_rtds,
                        "func": func,
                        "img_bpc": img_bpc,
                        "img_apc": img_apc,
                        "img_atst": img_atst,
                    })

                samples[key] = samples_for_test

        _r_conf = getattr(self, "_qual_confidence", 0.90)
        _r_ltpd = getattr(self, "_qual_ltpd", 5.0)
        report = {
            "customer": customer, "product": product,
            "author": author, "part_type": pt,
            "part_label": PART_TYPE_LABELS.get(pt, "Active Device"),
            "part_desc": part_desc,
            "date": datetime.now().strftime("%B %d, %Y"),
            "entries": entries,
            "samples": samples,
            # Project qualification criteria (from Planner)
            "qual_ltpd":       _r_ltpd,
            "qual_confidence": _r_conf,
            "qual_r_req":      1.0 - _r_ltpd / 100.0,
            # Counts: pass/fail derived from stat_pass when status is Complete
            "n_pass": sum(1 for e in entries if e["sc"] == "co" and e.get("stat_pass") is True),
            "n_fail": sum(1 for e in entries if e["sc"] == "co" and e.get("stat_pass") is False),
            "n_co":   sum(1 for e in entries if e["sc"] == "co"),
            "n_ip":   sum(1 for e in entries if e["sc"] == "ip"),
            "n_ns":   sum(1 for e in entries if e["sc"] == "ns"),
            "n_char": sum(1 for e in entries if e["sc"] == "ch"),
            "total":  len(entries),
        }
        s["last_report"] = report
        self._render_report(report)

    # ── form ──────────────────────────────────────────────────────────────────

    def _render_form(self, tests: dict, saved_counts: dict = None):
        _, s = self.sess()
        pt = s.get("part_type", "ttv")
        # Allow subclasses to inject per-test sample counts via instance attr
        if saved_counts is None:
            saved_counts = getattr(self, "_saved_n", {})

        # Pre-compute min sample sizes for k=0..20 failures using project LTPD + confidence
        # Fall back to JESD47 defaults (95% R, 90% CL) when not in a project context
        _qual_ltpd       = getattr(self, "_qual_ltpd", 5.0)
        _qual_confidence = getattr(self, "_qual_confidence", 0.90)
        _r_req           = 1.0 - _qual_ltpd / 100.0
        min_n_table = [min_sample_size(_r_req, _qual_confidence, k) for k in range(21)]
        min_n_js = str(min_n_table)
        # Human-readable labels for JS badge tooltip
        _ltpd_pct_js   = _qual_ltpd
        _conf_pct_js   = round(_qual_confidence * 100, 1)
        _r_req_pct_js  = round(_r_req * 100, 1)

        status_opts_html = "".join(
            f'<option value="{sc}">{lbl}</option>'
            for sc, lbl, _ in STATUS_OPTS
        )
        status_opts_char_html = "".join(
            f'<option value="{sc}">{lbl}</option>'
            for sc, lbl, _ in STATUS_OPTS_CHAR
        )

        test_rows = ""
        for key, t in tests.items():
            # Prefer saved planner count, fall back to TESTS dict default.
            # saved_counts[key] may be a plain int/str (planner count) or a list
            # of sample dicts (stored from a previous report submission) — handle both.
            sz = str(t["sample_size"]) if t["sample_size"] else "5"
            if key in saved_counts and saved_counts[key]:
                val = saved_counts[key]
                if isinstance(val, list):
                    # Report-format: list of sample dicts → use length as count
                    sz = str(len(val)) if val else sz
                else:
                    try:
                        sz = str(int(val))
                    except (TypeError, ValueError):
                        pass  # keep default
            is_char = t.get("characterization_only", False)
            opts_html = status_opts_char_html if is_char else status_opts_html

            # Pre-compute conditional cell HTML (no backslash allowed inside f-strings)
            if is_char:
                k_cell = ""
                char_hint = '<div class="text-muted small mt-1" style="font-style:italic">Characterization only — record warpage result in field above</div>'
            else:
                k_cell = (
                    '<td class="align-middle sf-' + key + '" style="min-width:100px">'
                    '<div class="d-flex align-items-center gap-1">'
                    '<input type="number" class="form-control form-control-sm k-input" name="k_' + key + '"'
                    ' id="k_' + key + '" value="0" min="0" placeholder="k" data-key="' + key + '"'
                    ' oninput="updatePF(\'' + key + '\')">'
                    '<span id="pf_' + key + '" style="font-size:.72rem;font-weight:700;'
                    'white-space:nowrap;padding:2px 7px;border-radius:4px;'
                    'background:#f3f4f6;color:#6b7280">—</span>'
                    '</div>'
                    '</td>'
                )
                char_hint = ""

            # For characterization tests: replace n/k with a warpage result field
            if is_char:
                nk_cells = f"""
              <td class="align-middle sf-{key}" colspan="2" style="min-width:160px">
                <input type="text" class="form-control form-control-sm" name="char_result_{key}"
                       id="n_{key}" placeholder="e.g. +125 µm at 260°C" data-key="{key}"
                       title="Record peak warpage result (signed, with units)">
              </td>"""
            else:
                nk_cells = f"""
              <td class="align-middle sf-{key}" style="min-width:80px">
                <input type="number" class="form-control form-control-sm n-input" name="n_{key}"
                       id="n_{key}" value="{sz}" min="1" placeholder="n" data-key="{key}"
                       oninput="updatePF('{key}')">
              </td>"""

            spec_url  = SPEC_URLS.get(key, "")
            std_cell  = (f'<a href="{spec_url}" target="_blank" rel="noopener" '
                         f'class="text-muted text-decoration-none" title="Open JEDEC spec">'
                         f'{t["standard"]} <i class="bi bi-box-arrow-up-right" style="font-size:.65rem"></i></a>'
                         ) if spec_url else t["standard"]

            test_rows += f"""
            <tr id="row-{key}">
              <td class="align-middle fw-semibold" style="min-width:170px">{t['name']}</td>
              <td class="align-middle text-muted small">{std_cell}</td>
              <td class="align-middle" style="min-width:140px">
                <select class="form-select form-select-sm status-sel" name="status_{key}" data-key="{key}" {"data-char=true" if is_char else ""}>
                  {opts_html}
                </select>
              </td>
              {nk_cells}
              {k_cell}
              <td>
                <input type="text" class="form-control form-control-sm" name="notes_{key}" placeholder="optional">
                {char_hint}
                <div id="warn-{key}" class="text-danger small mt-1" style="display:none">
                  <i class="bi bi-exclamation-triangle-fill me-1"></i>
                  <span id="warn-msg-{key}"></span>
                </div>
              </td>
            </tr>"""

        form_action = getattr(self, "_form_action", "/report")
        body = f"""
        <h4 class="mb-4" style="font-weight:300">Qualification Report</h4>
        <form method="post" action="{form_action}" enctype="multipart/form-data" id="report-form">
          <!-- Customer info -->
          <div class="card mb-4">
            <div class="card-df"><h6 class="mb-0">Report Information</h6></div>
            <div class="card-body p-4">
              <div class="row g-3">
                <div class="col-md-4">
                  <label class="form-label">Customer Name</label>
                  <input type="text" class="form-control" name="customer" placeholder="Customer">
                </div>
                <div class="col-md-4">
                  <label class="form-label">Product / Program</label>
                  <input type="text" class="form-control" name="product" value="SCD-on-Si Package">
                </div>
                <div class="col-md-4">
                  <label class="form-label">Prepared By</label>
                  <input type="text" class="form-control" name="author" placeholder="e.g. Reliability Engineering">
                </div>
              </div>
            </div>
          </div>
          <!-- Part Description -->
          <div class="card mb-4">
            <div class="card-df"><h6 class="mb-0">Part Description</h6></div>
            <div class="card-body p-4">"""

        if pt == "ttv":
            body += f"""
              <p style="font-style:italic;color:var(--df-grey);margin-bottom:1rem;font-size:.85rem">Nanotest TTV10-NT20 — 24.9 × 24.9mm flip-chip package, SAC305 solder on PCB. Die has 16 RTD temperature sensors (T1–T16), 4 heater zones, and 6 hotspot cells.</p>
              <div class="row g-3">
                <div class="col-md-4">
                  <label class="form-label">Si Die Thickness (µm)</label>
                  <input type="text" class="form-control" name="part_si_thick" value="50">
                </div>
                <div class="col-md-4">
                  <label class="form-label">Bond Technique</label>
                  <select class="form-select" name="part_bond_type">
                    <option value="Cu TCB">Cu TCB</option>
                    <option value="Ag Sinter" selected>Ag Sinter</option>
                    <option value="Ag TCB">Ag TCB</option>
                  </select>
                </div>
                <div class="col-md-4">
                  <label class="form-label">SCD Thickness (µm)</label>
                  <input type="text" class="form-control" name="part_diamond_thick" value="300">
                </div>
              </div>"""
        else:
            body += f"""
              <p style="color:var(--df-grey);margin-bottom:1rem;font-size:.85rem">Active device — part details are customer-specific.</p>
              <div class="row g-3">
                <div class="col-md-4">
                  <label class="form-label">Part Number</label>
                  <input type="text" class="form-control" name="part_number" placeholder="">
                </div>
                <div class="col-md-4">
                  <label class="form-label">Technology Node</label>
                  <input type="text" class="form-control" name="part_tech" placeholder="">
                </div>
                <div class="col-md-4">
                  <label class="form-label">Package Type</label>
                  <input type="text" class="form-control" name="part_package" placeholder="">
                </div>
              </div>"""

        body += f"""
            </div>
          </div>
          <!-- Test status table -->
          <div class="card mb-4">
            <div class="card-df"><h6 class="mb-0">Test Status</h6></div>
            <div class="table-responsive">
              <table class="table table-sm table-hover align-middle mb-0" style="min-width:700px">
                <thead class="tbl-header">
                  <tr>
                    <th>Test</th>
                    <th>Standard</th>
                    <th>Status</th>
                    <th title="Samples tested">n</th>
                    <th title="Failures observed">failures</th>
                    <th>Notes</th>
                  </tr>
                </thead>
                <tbody>{test_rows}</tbody>
              </table>
            </div>
          </div>
          <!-- Sample Records (TTV only) -->"""

        if pt == "ttv":
            body += f"""
          <div class="card mb-4">
            <div class="card-df"><h6 class="mb-0">Sample Records</h6></div>
            <div class="card-body p-4">
              <p style="font-size:.85rem;color:var(--df-grey);margin-bottom:1rem">
                <strong>CSAM:</strong> ≥95% bond area at all three timepoints.&ensp;
                <strong>Functionality:</strong> sensor reading within 5% of original (no heating).&ensp;
                <strong>Thermal:</strong> sensor reading within 5% of original (with heating).
              </p>"""

            for key, t in tests.items():
                body += f"""
              <div class="accordion mb-3" style="border:1px solid var(--df-border)">
                <div class="accordion-item">
                  <h2 class="accordion-header">
                    <button class="accordion-button collapsed py-2" type="button" data-bs-toggle="collapse" data-bs-target="#acc_{key}">
                      <strong>{t['name']}</strong>
                    </button>
                  </h2>
                  <div id="acc_{key}" class="accordion-collapse collapse">
                    <div class="accordion-body pt-3 pb-2">
                      <div class="row g-2 align-items-end mb-3">
                        <div class="col-auto">
                          <label class="form-label mb-2">Number of samples</label>
                          <input type="number" class="form-control form-control-sm n-samples-input" name="n_samples_{key}" value="{t.get('sample_size', 0)}" min="0" max="20" data-key="{key}" style="width:100px">
                        </div>
                        <div class="col-auto">
                          <button type="button" class="btn btn-sm btn-outline-secondary" onclick="buildSampleRows('{key}', parseInt(document.querySelector('[name=\\'n_samples_{key}\\']').value) || 0)">
                            Generate Rows
                          </button>
                        </div>
                      </div>
                      <div class="table-responsive" style="font-size:.8rem">
                        <table class="table table-sm table-bordered mb-0">
                          <thead class="tbl-header">
                            <tr>
                              <th style="min-width:100px">Sample ID</th>
                              <th style="min-width:110px">CSAM Before PC (%)</th>
                              <th style="min-width:110px">CSAM After PC (%)</th>
                              <th style="min-width:110px">CSAM After Test (%)</th>
                              <th style="min-width:70px">Thermal</th>
                              <th style="min-width:120px">Failed Sensors</th>
                              <th style="min-width:70px">Func</th>
                              <th style="min-width:240px">Images</th>
                            </tr>
                          </thead>
                          <tbody id="stbody_{key}">
                          </tbody>
                        </table>
                      </div>
                    </div>
                  </div>
                </div>
              </div>"""

            body += f"""
            </div>
          </div>
          <input type="hidden" id="part-type-input" value="{pt}">
          <script>
            function buildSampleRows(key, n) {{
              const tbody = document.getElementById('stbody_' + key);
              if (!tbody) return;
              tbody.innerHTML = '';
              for (let i = 0; i < n; i++) {{
                const sid = 'SN-' + String(i+1).padStart(3,'0');
                tbody.innerHTML += `<tr>
                  <td><input type="text" class="form-control form-control-sm" name="sid_${{key}}_${{i}}" value="${{sid}}"></td>
                  <td><input type="number" class="form-control form-control-sm" name="csam_bpc_${{key}}_${{i}}" step="0.1" min="0" max="100" placeholder="—"></td>
                  <td><input type="number" class="form-control form-control-sm" name="csam_apc_${{key}}_${{i}}" step="0.1" min="0" max="100" placeholder="—"></td>
                  <td><input type="number" class="form-control form-control-sm" name="csam_atst_${{key}}_${{i}}" step="0.1" min="0" max="100" placeholder="—"></td>
                  <td><select class="form-select form-select-sm" name="thermal_${{key}}_${{i}}"><option value="pass">Pass</option><option value="fail">Fail</option></select></td>
                  <td><input type="text" class="form-control form-control-sm" name="failed_rtds_${{key}}_${{i}}" placeholder="e.g. T3,T7"></td>
                  <td><select class="form-select form-select-sm" name="func_${{key}}_${{i}}"><option value="pass">Pass</option><option value="fail">Fail</option></select></td>
                  <td style="min-width:240px">
                    <div class="mb-1"><label style="font-size:.7rem">Pre-PC</label><input type="file" class="form-control form-control-sm" name="img_bpc_${{key}}_${{i}}" accept="image/*"></div>
                    <div class="mb-1"><label style="font-size:.7rem">Post-PC</label><input type="file" class="form-control form-control-sm" name="img_apc_${{key}}_${{i}}" accept="image/*"></div>
                    <div><label style="font-size:.7rem">Post-Test</label><input type="file" class="form-control form-control-sm" name="img_atst_${{key}}_${{i}}" accept="image/*"></div>
                  </td>
                </tr>`;
              }}
            }}

            // Auto-init: listen to n_samples inputs
            document.querySelectorAll('.n-samples-input').forEach(inp => {{
              const key = inp.dataset.key;
              buildSampleRows(key, parseInt(inp.value) || 0);
              inp.addEventListener('change', function() {{
                buildSampleRows(key, parseInt(this.value) || 0);
              }});
            }});
          </script>"""

        else:
            body += f"""
          <div class="card mb-4">
            <div class="card-body p-4">
              <p style="font-size:.85rem;color:var(--df-grey)">Sample tracking not yet configured for active devices.</p>
            </div>
          </div>"""

        body += f"""
          <button type="submit" class="btn btn-primary btn-lg px-5">
            <i class="bi bi-file-earmark-check me-2"></i>Generate Report
          </button>
        </form>

        <script>
        // Pre-computed min sample sizes using project LTPD ({_ltpd_pct_js}%) and
        // confidence ({_conf_pct_js}%): MIN_N[k] = min n to demonstrate ≤{_ltpd_pct_js}% defective
        const MIN_N = {min_n_js};
        const QUAL_LTPD_PCT  = {_ltpd_pct_js};
        const QUAL_CONF_PCT  = {_conf_pct_js};
        const QUAL_R_PCT     = {_r_req_pct_js};

        // updatePF: compute and display automatic Pass/Fail badge from n and k inputs
        function updatePF(key) {{
          const badge = document.getElementById('pf_' + key);
          if (!badge) return;  // characterization test, no badge
          const nEl = document.getElementById('n_' + key);
          const kEl = document.getElementById('k_' + key);
          if (!nEl || !kEl) return;
          const n = parseInt(nEl.value) || 0;
          const k = parseInt(kEl.value) || 0;
          if (n === 0) {{
            badge.textContent = '—';
            badge.style.background = '#f3f4f6';
            badge.style.color = '#6b7280';
            badge.title = '';
            return;
          }}
          const minN = (k < MIN_N.length) ? MIN_N[k] : MIN_N[MIN_N.length - 1];
          badge.title = `Need n≥${{minN}} to pass (≤${{QUAL_LTPD_PCT}}% defective @ ${{QUAL_CONF_PCT}}% CL)`;
          if (n >= minN) {{
            badge.textContent = 'PASS';
            badge.style.background = '#dcfce7';
            badge.style.color = '#15803d';
          }} else {{
            badge.textContent = 'FAIL';
            badge.style.background = '#fee2e2';
            badge.style.color = '#b91c1c';
          }}
        }}

        function refreshFields() {{
          document.querySelectorAll('.status-sel').forEach(sel => {{
            const key = sel.dataset.key;
            const isChar = sel.dataset.char === 'true';
            // Show n/k cells when status is in-progress, complete, or characterized
            const show = isChar ? ['ip','ch'].includes(sel.value) : ['ip','co'].includes(sel.value);
            document.querySelectorAll('.sf-' + key).forEach(cell => {{
              cell.style.opacity = show ? '1' : '0.4';
              cell.querySelectorAll('input').forEach(i => i.disabled = !show);
            }});
            if (!isChar) updatePF(key);
          }});
        }}

        document.querySelectorAll('.status-sel').forEach(s => {{
          s.addEventListener('change', function() {{
            const key = this.dataset.key;
            const isChar = this.dataset.char === 'true';
            const show = isChar ? ['ip','ch'].includes(this.value) : ['ip','co'].includes(this.value);
            document.querySelectorAll('.sf-' + key).forEach(cell => {{
              cell.style.opacity = show ? '1' : '0.4';
              cell.querySelectorAll('input').forEach(i => i.disabled = !show);
            }});
            if (!isChar) updatePF(key);
          }});
        }});

        refreshFields();
        </script>"""
        self.emit(body, "Qual Report", "report")

    # ── report display ────────────────────────────────────────────────────────

    def _render_report(self, r: dict):
        # Banner for any auto-corrected entries
        corrected_entries = [e for e in r["entries"] if e.get("corrected")]
        correction_banner = ""
        if corrected_entries:
            names = ", ".join(e["test"]["name"] for e in corrected_entries)
            correction_banner = f"""
            <div class="alert alert-warning d-flex align-items-start gap-2 mb-4" role="alert">
              <i class="bi bi-exclamation-triangle-fill fs-5 mt-1 flex-shrink-0"></i>
              <div>
                <strong>Status corrected on {len(corrected_entries)} test(s):</strong> {names}<br>
                <small>These were submitted as <em>Pass</em> but the n / failures values do not demonstrate
                ≥95% reliability at 90% confidence (JEDEC chi-squared method). Status has been
                overridden to <strong>Fail</strong>.</small>
              </div>
            </div>"""

        # Summary count tiles
        tiles = ""
        for count, label, color in [
            (r["n_pass"],             "PASS",           "success"),
            (r["n_fail"],             "FAIL",           "danger"),
            (r.get("n_co", 0),        "COMPLETE",       "success"),
            (r["n_ip"],               "IN PROGRESS",    "warning"),
            (r["n_ns"],               "NOT STARTED",    "secondary"),
            (r.get("n_char", 0),      "CHARACTERIZED",  "info"),
        ]:
            # Only show PASS/FAIL tiles when some tests are complete with stats
            if label in ("PASS", "FAIL") and r.get("n_co", 0) == 0:
                continue
            if count == 0 and label in ("CHARACTERIZED", "PASS", "FAIL"):
                continue  # hide tile if count is zero
            tiles += f"""
            <div class="col d-flex flex-column align-items-center justify-content-center py-3" style="gap:.4rem">
              <div class="stat-num text-{color}">{count}</div>
              <div class="text-muted" style="font-size:.72rem;line-height:1">{label}</div>
            </div>"""

        # Summary table rows
        table_rows = ""
        for e in r["entries"]:
            is_char = e.get("is_char", False)
            color = STATUS_COLOR.get(e["sc"], "secondary")
            badge = f'<span class="badge bg-{color} {"text-dark" if color in ("light","info") else ""}">{e["status"]}</span>'

            if is_char:
                # Characterization test: show warpage result instead of n/k and stat
                char_res = e.get("char_result", "") or "—"
                nk = "—"
                stat_cell = f' <small class="text-muted">{char_res}</small>'
            else:
                nk = f"{e['n']} / {e['k']}" if e["n"] is not None else "—"
                stat_cell = ""
                if e["r_demo"] is not None:
                    ok       = e["stat_pass"]
                    res      = "PASS" if ok else "FAIL"
                    _cl_lbl  = f'{r.get("qual_confidence", 0.90)*100:.0f}% CL'
                    stat_cell = f' <small class="text-muted">({e["r_demo"]*100:.1f}% R @ {_cl_lbl} — <span class="{"text-success" if ok else "text-danger"}">{res}</span>)</small>'
            notes_td = f'<td class="text-muted small">{e["notes"]}</td>' if e["notes"] else '<td class="text-muted">—</td>'
            table_rows += f"""
            <tr>
              <td class="fw-semibold">{e['test']['name']}</td>
              <td class="text-muted small">{e['test']['standard']}</td>
              <td>{badge}{stat_cell}</td>
              <td class="text-muted small">{nk}</td>
              {notes_td}
            </tr>"""

        # Statistical details — pull project criteria from report dict
        _q_ltpd = r.get("qual_ltpd", 5.0)
        _q_conf = r.get("qual_confidence", 0.90)
        _q_rreq = r.get("qual_r_req", 0.95)
        _q_conf_lbl = f'{_q_conf*100:.0f}%'
        _q_rreq_lbl = f'{_q_rreq*100:.0f}%'
        _q_ltpd_lbl = f'{_q_ltpd:g}%'
        stat_entries = [e for e in r["entries"] if e["r_demo"] is not None]
        stat_section = ""
        if stat_entries:
            srows = ""
            for e in stat_entries:
                ok  = e["stat_pass"]
                srows += (
                    f'<tr><td class="fw-semibold">{e["test"]["name"]}</td>'
                    f'<td>{e["n"]}</td><td>{e["k"]}</td>'
                    f'<td>{e["r_demo"]*100:.2f}%</td>'
                    f'<td><span class="badge {"bg-success" if ok else "bg-danger"}">'
                    f'{"PASS" if ok else "FAIL"}</span> vs {_q_rreq_lbl} R @ {_q_conf_lbl} CL</td></tr>'
                )
            stat_section = f"""
            <h6 class="text-muted mt-4 mb-2" style="font-size:.8rem;letter-spacing:.05em;text-transform:uppercase">Statistical Details ({_q_conf_lbl} confidence, {_q_rreq_lbl} reliability target — LTPD {_q_ltpd_lbl})</h6>
            <table class="table table-sm table-bordered">
              <thead class="tbl-header"><tr><th>Test</th><th>n</th><th>Failures</th><th>Demonstrated R</th><th>Result</th></tr></thead>
              <tbody>{srows}</tbody>
            </table>"""

        body = f"""
        <!-- Action bar -->
        <div class="d-flex align-items-center justify-content-between mb-4 no-print">
          <h4 class="mb-0" style="font-weight:300;letter-spacing:-.01em">Qualification Report</h4>
          <div class="d-flex gap-2">
            <a href="/report/pdf" class="btn btn-primary">Download PDF &darr;</a>
            <button onclick="window.print()" class="btn btn-outline-secondary">Print</button>
            <a href="/report" class="btn btn-outline-primary">New Report</a>
          </div>
        </div>

        <!-- Report body -->
        <div class="report-wrap">
          <div class="rpt-header">
            <div class="d-flex justify-content-between align-items-start">
              <div>
                <div class="rpt-title">Reliability Qualification Report</div>
              </div>
              <div class="rpt-sub text-end">{r['date']}</div>
            </div>
          </div>

          <!-- Metadata -->
          <div class="row g-3 mb-4">
            <div class="col-6 col-md-3">
              <div class="text-muted small">Customer</div>
              <div class="fw-semibold">{r['customer']}</div>
            </div>
            <div class="col-6 col-md-3">
              <div class="text-muted small">Product</div>
              <div class="fw-semibold">{r['product']}</div>
            </div>
            <div class="col-6 col-md-3">
              <div class="text-muted small">Part Type</div>
              <div class="fw-semibold">{r['part_label']}</div>
            </div>
            <div class="col-6 col-md-3">
              <div class="text-muted small">Prepared By</div>
              <div class="fw-semibold">{r['author']}</div>
            </div>
          </div>

          <!-- Part Description -->"""

        if r['part_type'] == 'ttv':
            pd = r.get('part_desc', {})
            si_thick = pd.get('si_thick', '50')
            bond_type = pd.get('bond_type', 'Ag Sinter')
            diamond_thick = pd.get('diamond_thick', '300')
            body += f"""
          <div style="background:var(--df-bg);border-left:3px solid var(--df-border);padding:.75rem 1rem;margin-bottom:1.5rem;font-size:.85rem">
            <div style="font-weight:600;margin-bottom:.5rem">Nanotest TTV10-NT20 Thermal Test Vehicle</div>
            <div style="color:var(--df-grey);margin-bottom:.25rem">25mm die package — flip-chip on PCB, SAC305 solder</div>
            <div style="color:var(--df-grey);margin-bottom:.5rem">Si die: {si_thick} µm | Bond: {bond_type} | SCD: {diamond_thick} µm</div>
            <div style="color:var(--df-grey);font-size:.8rem">16 RTD sensors (T1–T16) | 4 heater zones (S1–S4) | 6 hotspot cells (HS1–HS6)</div>
          </div>"""
        else:
            pd = r.get('part_desc', {})
            pn = pd.get('part_number', '—')
            pt_node = pd.get('part_tech', '—')
            pkg = pd.get('part_package', '—')
            body += f"""
          <div style="background:var(--df-bg);border-left:3px solid var(--df-border);padding:.75rem 1rem;margin-bottom:1.5rem;font-size:.85rem">
            <div style="font-weight:600;margin-bottom:.5rem">Active Device</div>
            <div style="color:var(--df-grey);margin-bottom:.25rem">Part Number: {pn}</div>
            <div style="color:var(--df-grey);margin-bottom:.25rem">Technology Node: {pt_node}</div>
            <div style="color:var(--df-grey)">Package Type: {pkg}</div>
          </div>"""

        body += f"""
          <!-- Precond note -->
          <div class="precond-bar mb-4">
            <strong>Precursor for all tests:</strong>
            PC ({PRECOND['full_name']}; {PRECOND['standard']}) &mdash;
            {PRECOND['condition']} &mdash; {PRECOND['duration']}
          </div>

          {correction_banner}

          <!-- Summary counts -->
          <div class="row g-0 mb-4 text-center border rounded overflow-hidden">
            {tiles}
          </div>

          <!-- Summary table -->
          <table class="table table-bordered table-hover table-sm mb-2">
            <thead class="tbl-header">
              <tr><th>Test</th><th>Standard</th><th>Status</th><th>n / fails</th><th>Notes</th></tr>
            </thead>
            <tbody>{table_rows}</tbody>
          </table>

          {stat_section}

          <!-- Sample Records Tables (TTV only) -->"""

        samples = r.get("samples", {})
        if r['part_type'] == 'ttv' and samples:
            for key in samples:
                test_samples = samples[key]
                if not test_samples:
                    continue

                test_name = None
                for e in r["entries"]:
                    if e["key"] == key:
                        test_name = e["test"]["name"]
                        break

                if not test_name:
                    continue

                body += f"""
          <h6 class="text-muted mt-4 mb-2" style="font-size:.8rem;letter-spacing:.05em;text-transform:uppercase">{test_name} — Sample Records</h6>
          <table class="table table-sm table-bordered mb-3">
            <thead class="tbl-header">
              <tr>
                <th style="min-width:100px">Sample</th>
                <th style="min-width:120px">CSAM Before PC</th>
                <th style="min-width:120px">CSAM After PC</th>
                <th style="min-width:120px">CSAM After Test</th>
                <th style="min-width:100px">CSAM Status</th>
                <th style="min-width:80px">Thermal</th>
                <th style="min-width:80px">Func</th>
                <th style="min-width:80px">Overall</th>
              </tr>
            </thead>
            <tbody>"""

                for sample in test_samples:
                    sample_id = sample["id"]
                    csam_bpc = sample["csam_bpc"]
                    csam_apc = sample["csam_apc"]
                    csam_atst = sample["csam_atst"]
                    csam_status = sample["csam_status"]
                    csam_badge = sample["csam_badge"]
                    thermal = sample["thermal"]
                    func = sample["func"]
                    failed_rtds = sample["failed_rtds"]

                    # Determine overall status
                    if csam_status == "Rejected — pre-PC < 95%":
                        overall = "Rejected"
                        overall_badge = "danger"
                        show_dash = True
                    else:
                        show_dash = False
                        if csam_status == "Pass" and thermal == "pass" and func == "pass":
                            overall = "Pass"
                            overall_badge = "success"
                        else:
                            overall = "Fail"
                            overall_badge = "danger"

                    body += f"""
              <tr>
                <td class="fw-semibold">{sample_id}</td>
                <td class="text-muted small">{f"{csam_bpc:.1f}%" if csam_bpc is not None else "—"}</td>
                <td class="text-muted small">{f"{csam_apc:.1f}%" if csam_apc is not None else "—"}</td>
                <td class="text-muted small">{f"{csam_atst:.1f}%" if csam_atst is not None else "—"}</td>
                <td><span class="badge bg-{csam_badge}">{csam_status}</span></td>
                <td class="text-muted small">{"—" if show_dash else thermal.upper()}</td>
                <td class="text-muted small">{"—" if show_dash else func.upper()}</td>
                <td><span class="badge bg-{overall_badge}">{overall.upper()}</span></td>
              </tr>"""

                body += f"""
            </tbody>
          </table>"""

        # CSAM Images Appendix
        has_images = False
        for key in samples:
            for sample in samples.get(key, []):
                if sample["img_bpc"] or sample["img_apc"] or sample["img_atst"]:
                    has_images = True
                    break
            if has_images:
                break

        if r['part_type'] == 'ttv' and has_images:
            body += f"""
          <h6 class="text-muted mt-5 mb-3" style="font-size:.8rem;letter-spacing:.05em;text-transform:uppercase">Appendix — CSAM Images</h6>"""

            for key in samples:
                test_samples = samples[key]
                test_name = None
                for e in r["entries"]:
                    if e["key"] == key:
                        test_name = e["test"]["name"]
                        break

                if not test_name:
                    continue

                # Check if any sample in this test has images
                test_has_images = any(s["img_bpc"] or s["img_apc"] or s["img_atst"] for s in test_samples)
                if not test_has_images:
                    continue

                body += f"""
          <div style="margin-top:1.5rem;page-break-inside:avoid">
            <div style="font-size:.85rem;font-weight:600;margin-bottom:.75rem;color:var(--df-black)">{test_name}</div>"""

                for sample in test_samples:
                    sample_id = sample["id"]
                    img_bpc = sample["img_bpc"]
                    img_apc = sample["img_apc"]
                    img_atst = sample["img_atst"]

                    if not (img_bpc or img_apc or img_atst):
                        continue

                    body += f"""
            <div style="margin-bottom:1.5rem">
              <div style="font-size:.8rem;color:var(--df-grey);margin-bottom:.5rem;font-weight:500">{sample_id}</div>
              <div style="display:flex;gap:1rem;align-items:flex-start">"""

                    for label, img_uri, csam_val in [("Before PC", img_bpc, sample["csam_bpc"]), ("After PC", img_apc, sample["csam_apc"]), ("After Test", img_atst, sample["csam_atst"])]:
                        if img_uri:
                            csam_text = f"{csam_val:.1f}%" if csam_val is not None else ""
                            body += f"""
                <div style="flex:1;text-align:center">
                  <img src="{img_uri}" style="max-width:250px;border:1px solid #e0e0e0;margin-bottom:.5rem">
                  <div style="font-size:.75rem;color:var(--df-grey)">{label}</div>
                  <div style="font-size:.7rem;color:var(--df-grey)">{csam_text}</div>
                </div>"""

                    body += f"""
              </div>
            </div>"""

                body += f"""
          </div>"""

        body += f"""
          <div class="text-muted text-end mt-4" style="font-size:.75rem">
            Generated by Package Reliability Qualification Suite &mdash; {r['date']}
          </div>
        </div>"""
        self.emit(body, "Qual Report", "report")


# ── /report/pdf ───────────────────────────────────────────────────────────────

class ReportPdfHandler(Base):
    def get(self):
        import traceback
        _, s = self.sess()
        rpt = s.get("last_report")
        if not rpt:
            self.redirect("/report")
            return
        try:
            pdf_bytes = _make_pdf(rpt)
        except Exception:
            err = traceback.format_exc()
            self.set_status(500)
            self.set_header("Content-Type", "text/plain")
            self.finish(f"PDF generation error:\n\n{err}")
            return
        fname = f"QualReport_{rpt['customer'].replace(' ','_')}_{datetime.now().strftime('%Y%m%d')}.pdf"
        self.set_header("Content-Type", "application/pdf")
        self.set_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.finish(pdf_bytes)


# ── PDF generation (ReportLab) ────────────────────────────────────────────────

_NAVY   = colors.HexColor("#0f2744")
_LIGHT  = colors.HexColor("#e8f0fe")
_PASS_C = colors.HexColor("#d1e7dd")
_FAIL_C = colors.HexColor("#f8d7da")
_IP_C   = colors.HexColor("#fff3cd")
_NS_C   = colors.HexColor("#e9ecef")

_STATUS_BG = {"PASS": _PASS_C, "FAIL": _FAIL_C,
              "IN PROGRESS": _IP_C, "NOT STARTED": _NS_C, "N/A": _NS_C}


def _b64_to_rl_image(b64_uri: str, w: float, h: float):
    """Convert a base64 data URI to a ReportLab Image flowable."""
    try:
        header, data = b64_uri.split(",", 1)
        img_bytes = base64.b64decode(data)
        return RLImage(io.BytesIO(img_bytes), width=w, height=h)
    except Exception:
        return None


def _make_pdf(r: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                             topMargin=2*cm, bottomMargin=2*cm,
                             leftMargin=2*cm, rightMargin=2*cm)
    styles = getSampleStyleSheet()

    # Custom paragraph styles
    def S(name, **kw):
        base = kw.pop("parent", "Normal")
        return ParagraphStyle(name, parent=styles[base], **kw)

    hdr_title  = S("HT", fontSize=16, fontName="Helvetica-Bold",
                   textColor=colors.white, spaceAfter=7, leading=20)
    hdr_sub    = S("HS", fontSize=9,  textColor=colors.HexColor("#aac4e0"),
                   leading=13, spaceBefore=2)
    meta_label = S("ML", fontSize=8,  textColor=colors.grey)
    meta_val   = S("MV", fontSize=10, fontName="Helvetica-Bold")
    body_s     = S("B",  fontSize=9)
    small_s    = S("Sm", fontSize=8,  textColor=colors.grey)
    section_h  = S("SH", fontSize=10, fontName="Helvetica-Bold",
                   textColor=_NAVY, spaceBefore=12, spaceAfter=4)
    sub_h      = S("SUB", fontSize=9, fontName="Helvetica-Bold",
                   textColor=colors.HexColor("#444444"), spaceBefore=8, spaceAfter=2)
    img_cap    = S("IC", fontSize=7, textColor=colors.grey, alignment=TA_CENTER)

    W = 17 * cm   # usable width

    story = []

    # ── Helper: page-wide navy header bar (reused on each page break) ──────────
    def _hdr_bar():
        d = [[
            [Paragraph("RELIABILITY QUALIFICATION REPORT", hdr_title)],
            [Paragraph(r["date"], hdr_sub)],
        ]]
        t = Table(d, colWidths=[12*cm, 5*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,-1), _NAVY),
            ("TOPPADDING",   (0,0), (-1,-1), 16),
            ("BOTTOMPADDING",(0,0), (-1,-1), 16),
            ("LEFTPADDING",  (0,0), (-1,-1), 16),
            ("VALIGN",       (0,0), (-1,-1), "TOP"),
            ("ALIGN",        (1,0), (1,0),   "RIGHT"),
        ]))
        return t

    # ── 1. Header bar ─────────────────────────────────────────────────────────
    story.append(_hdr_bar())
    story.append(Spacer(1, 0.5*cm))

    # ── 2. Metadata block ─────────────────────────────────────────────────────
    meta_rows = [
        [Paragraph("Customer",   meta_label), Paragraph(r["customer"],  meta_val),
         Paragraph("Product",    meta_label), Paragraph(r["product"],   meta_val)],
        [Paragraph("Part Type",  meta_label), Paragraph(r["part_label"],meta_val),
         Paragraph("Prepared By",meta_label), Paragraph(r["author"],    meta_val)],
    ]
    # Part description rows
    pd = r.get("part_desc", {})
    if r.get("part_type") == "ttv":
        meta_rows.append([
            Paragraph("Si Thickness",      meta_label), Paragraph(pd.get("si_thick","—") + " µm", meta_val),
            Paragraph("Bond Type",         meta_label), Paragraph(pd.get("bond_type","—"),         meta_val),
        ])
        meta_rows.append([
            Paragraph("SCD Thickness",     meta_label), Paragraph(pd.get("diamond_thick","—") + " µm", meta_val),
            Paragraph("",                  meta_label), Paragraph("",                                   meta_val),
        ])
    else:
        meta_rows.append([
            Paragraph("Part Number",  meta_label), Paragraph(pd.get("part_number","—"), meta_val),
            Paragraph("Tech Node",    meta_label), Paragraph(pd.get("part_tech","—"),   meta_val),
        ])
        meta_rows.append([
            Paragraph("Package",      meta_label), Paragraph(pd.get("part_package","—"), meta_val),
            Paragraph("",             meta_label), Paragraph("",                          meta_val),
        ])
    meta_tbl = Table(meta_rows, colWidths=[2.5*cm, 6*cm, 2.5*cm, 6*cm])
    meta_tbl.setStyle(TableStyle([
        ("TOPPADDING",    (0,0),(-1,-1), 3),
        ("BOTTOMPADDING", (0,0),(-1,-1), 3),
    ]))
    story.append(meta_tbl)
    story.append(Spacer(1, 0.4*cm))
    story.append(HRFlowable(width=W, color=colors.HexColor("#dee2e6")))
    story.append(Spacer(1, 0.4*cm))

    # ── 3. Precond callout ────────────────────────────────────────────────────
    precond_data = [[
        Paragraph(
            f"<b>Universal Precursor for all tests:</b> "
            f"PC ({PRECOND['full_name']}; {PRECOND['standard']}) — "
            f"{PRECOND['condition']} — {PRECOND['duration']}",
            S("PC", fontSize=8, textColor=colors.HexColor("#5a4000"))
        )
    ]]
    precond_tbl = Table(precond_data, colWidths=[W])
    precond_tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0,0),(-1,-1), colors.HexColor("#fff8e1")),
        ("LEFTPADDING",  (0,0),(-1,-1), 10),
        ("TOPPADDING",   (0,0),(-1,-1), 8),
        ("BOTTOMPADDING",(0,0),(-1,-1), 8),
    ]))
    story.append(precond_tbl)
    story.append(Spacer(1, 0.5*cm))

    # ── 4. Summary counts ─────────────────────────────────────────────────────
    counts = [
        (r["n_pass"], "PASS",        colors.HexColor("#198754")),
        (r["n_fail"], "FAIL",        colors.HexColor("#dc3545")),
        (r["n_ip"],   "IN PROGRESS", colors.HexColor("#fd7e14")),
        (r["n_ns"],   "NOT STARTED", colors.grey),
        (r["total"],  "TOTAL",       _NAVY),
    ]
    col_w = W / len(counts)
    # Two separate rows: numbers on top, labels below
    num_row   = [Paragraph(str(c),  S(f"CN{i}", fontSize=22, fontName="Helvetica-Bold",
                                       textColor=color, alignment=TA_CENTER))
                 for i, (c, label, color) in enumerate(counts)]
    label_row = [Paragraph(label, S(f"CL{i}", fontSize=7, textColor=colors.grey,
                                     alignment=TA_CENTER))
                 for i, (c, label, color) in enumerate(counts)]
    cnt_tbl = Table([num_row, label_row],
                    colWidths=[col_w] * len(counts),
                    rowHeights=[1.1*cm, 0.55*cm])
    cnt_tbl.setStyle(TableStyle([
        ("TOPPADDING",    (0,0), (-1,0),  6),
        ("BOTTOMPADDING", (0,0), (-1,0),  6),
        ("TOPPADDING",    (0,1), (-1,1),  2),
        ("BOTTOMPADDING", (0,1), (-1,1),  6),
        ("BACKGROUND",    (0,0), (-1,-1), colors.HexColor("#f8f9fa")),
        ("BOX",           (0,0), (-1,-1), 0.5, colors.HexColor("#dee2e6")),
        ("VALIGN",        (0,0), (-1,0),  "MIDDLE"),
        ("VALIGN",        (0,1), (-1,1),  "TOP"),
    ]))
    story.append(cnt_tbl)
    story.append(Spacer(1, 0.5*cm))

    # ── 5. Summary table ──────────────────────────────────────────────────────
    story.append(Paragraph("Test Results Summary", section_h))
    th_s = S("TH", fontSize=9, fontName="Helvetica-Bold", textColor=colors.white)
    tbl_data = [[
        Paragraph("Test",      th_s),
        Paragraph("Standard",  th_s),
        Paragraph("Status",    th_s),
        Paragraph("n / fails", th_s),
        Paragraph("Notes",     th_s),
    ]]
    row_style_cmds = [
        ("BACKGROUND",    (0,0), (-1,0), _NAVY),
        ("GRID",          (0,0), (-1,-1), 0.4, colors.HexColor("#dee2e6")),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING",   (0,0), (-1,-1), 6),
        ("FONTSIZE",      (0,1), (-1,-1), 9),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
    ]
    for i, e in enumerate(r["entries"], start=1):
        if e.get("is_char"):
            nk = e.get("char_result") or "—"
        elif e["n"] is not None:
            nk = f"{e['n']} / {e['k']}"
        else:
            nk = "—"
        bg = _STATUS_BG.get(e["status"], _NS_C)
        tbl_data.append([
            Paragraph(e["test"]["name"],     body_s),
            Paragraph(e["test"]["standard"], small_s),
            Paragraph(e["status"],           body_s),
            Paragraph(nk,                    body_s),
            Paragraph(e["notes"] or "—",     small_s),
        ])
        row_style_cmds.append(("BACKGROUND", (2, i), (2, i), bg))

    sum_tbl = Table(tbl_data, colWidths=[5*cm, 2.8*cm, 3.5*cm, 2.2*cm, 3.5*cm])
    sum_tbl.setStyle(TableStyle(row_style_cmds))
    story.append(sum_tbl)

    # ── 6. Statistical details ────────────────────────────────────────────────
    _pdf_ltpd = r.get("qual_ltpd", 5.0)
    _pdf_conf = r.get("qual_confidence", 0.90)
    _pdf_rreq = r.get("qual_r_req", 0.95)
    _pdf_conf_lbl = f'{_pdf_conf*100:.0f}%'
    _pdf_rreq_lbl = f'{_pdf_rreq*100:.0f}%'
    _pdf_ltpd_lbl = f'{_pdf_ltpd:g}%'
    stat_entries = [e for e in r["entries"] if e.get("r_demo") is not None]
    if stat_entries:
        story.append(Spacer(1, 0.5*cm))
        story.append(Paragraph(
            f"Statistical Details  ({_pdf_conf_lbl} confidence level, {_pdf_rreq_lbl} reliability target — LTPD {_pdf_ltpd_lbl})", section_h))
        s_data = [[
            Paragraph(h, S(f"SH{j}", fontSize=9, fontName="Helvetica-Bold",
                           textColor=colors.white))
            for j, h in enumerate(["Test", "n", "Failures",
                                    "Demonstrated R", f"vs {_pdf_rreq_lbl} Target"])
        ]]
        s_cmds = [
            ("BACKGROUND",    (0,0), (-1,0), _NAVY),
            ("GRID",          (0,0), (-1,-1), 0.4, colors.HexColor("#dee2e6")),
            ("TOPPADDING",    (0,0), (-1,-1), 5),
            ("BOTTOMPADDING", (0,0), (-1,-1), 5),
            ("LEFTPADDING",   (0,0), (-1,-1), 6),
            ("FONTSIZE",      (0,1), (-1,-1), 9),
        ]
        for i, e in enumerate(stat_entries, start=1):
            ok  = e["stat_pass"]
            bg  = _PASS_C if ok else _FAIL_C
            res = "PASS" if ok else "FAIL"
            s_data.append([
                Paragraph(e["test"]["name"],          body_s),
                Paragraph(str(e["n"]),                body_s),
                Paragraph(str(e["k"]),                body_s),
                Paragraph(f"{e['r_demo']*100:.2f}%",  body_s),
                Paragraph(res,                        body_s),
            ])
            s_cmds.append(("BACKGROUND", (4, i), (4, i), bg))
        stat_tbl = Table(s_data, colWidths=[5.5*cm, 2*cm, 2*cm, 4*cm, 3.5*cm])
        stat_tbl.setStyle(TableStyle(s_cmds))
        story.append(stat_tbl)

    # ── 7. Sample records ─────────────────────────────────────────────────────
    all_samples = r.get("samples", {})
    tests_with_samples = [(e, all_samples[e["key"]])
                          for e in r["entries"]
                          if e["key"] in all_samples and all_samples[e["key"]]]
    if tests_with_samples:
        story.append(PageBreak())
        story.append(_hdr_bar())
        story.append(Spacer(1, 0.5*cm))
        story.append(Paragraph("Sample Records", section_h))

        sr_th = S("SRTH", fontSize=8, fontName="Helvetica-Bold", textColor=colors.white)
        sr_td = S("SRTD", fontSize=8)
        sr_sm = S("SRSM", fontSize=7, textColor=colors.grey)

        for entry, slist in tests_with_samples:
            story.append(Paragraph(entry["test"]["name"], sub_h))

            # Table header
            sr_data = [[
                Paragraph("Sample ID",          sr_th),
                Paragraph("CSAM Before PC (%)", sr_th),
                Paragraph("CSAM After PC (%)",  sr_th),
                Paragraph("CSAM After Test (%)",sr_th),
                Paragraph("CSAM Status",        sr_th),
                Paragraph("Thermal",            sr_th),
                Paragraph("Failed Sensors",     sr_th),
                Paragraph("Func.",              sr_th),
            ]]
            sr_cmds = [
                ("BACKGROUND",    (0,0), (-1,0), _NAVY),
                ("GRID",          (0,0), (-1,-1), 0.4, colors.HexColor("#dee2e6")),
                ("TOPPADDING",    (0,0), (-1,-1), 4),
                ("BOTTOMPADDING", (0,0), (-1,-1), 4),
                ("LEFTPADDING",   (0,0), (-1,-1), 5),
                ("FONTSIZE",      (0,1), (-1,-1), 8),
                ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
            ]
            for ri, s in enumerate(slist, start=1):
                def _pct(v):
                    return f"{v:.1f}%" if v is not None else "—"
                def _tf(v):
                    return v.upper() if v else "—"
                csam_ok = s.get("csam_status","") == "Pass"
                therm_ok = s.get("thermal","") == "pass"
                func_ok  = s.get("func","") == "pass"
                rtds = ", ".join(s.get("failed_rtds", [])) or "None"

                sr_data.append([
                    Paragraph(s["id"],                         sr_td),
                    Paragraph(_pct(s.get("csam_bpc")),         sr_td),
                    Paragraph(_pct(s.get("csam_apc")),         sr_td),
                    Paragraph(_pct(s.get("csam_atst")),        sr_td),
                    Paragraph(s.get("csam_status", "—"),       sr_td),
                    Paragraph(_tf(s.get("thermal")),           sr_td),
                    Paragraph(rtds,                            sr_sm),
                    Paragraph(_tf(s.get("func")),              sr_td),
                ])
                csam_bg = _PASS_C if csam_ok else _FAIL_C
                therm_bg = _PASS_C if therm_ok else _FAIL_C
                func_bg  = _PASS_C if func_ok  else _FAIL_C
                sr_cmds += [
                    ("BACKGROUND", (4, ri), (4, ri), csam_bg),
                    ("BACKGROUND", (5, ri), (5, ri), therm_bg),
                    ("BACKGROUND", (7, ri), (7, ri), func_bg),
                ]

            sr_tbl = Table(sr_data,
                           colWidths=[2.2*cm, 2.1*cm, 2.1*cm, 2.2*cm,
                                      3.2*cm, 1.6*cm, 1.8*cm, 1.6*cm])
            sr_tbl.setStyle(TableStyle(sr_cmds))
            story.append(sr_tbl)
            story.append(Spacer(1, 0.4*cm))

    # ── 8. CSAM Image Registry ────────────────────────────────────────────────
    csam_entries = []
    for entry, slist in tests_with_samples:
        for s in slist:
            if s.get("img_bpc") or s.get("img_apc") or s.get("img_atst"):
                csam_entries.append((entry, s))

    if csam_entries:
        story.append(PageBreak())
        story.append(_hdr_bar())
        story.append(Spacer(1, 0.5*cm))
        story.append(Paragraph("CSAM Image Registry", section_h))
        story.append(Paragraph(
            "Acoustic microscopy images for each sample — Before Preconditioning | "
            "After Preconditioning | After Test.  Bonded area threshold: ≥95%.",
            S("CSUB", fontSize=8, textColor=colors.grey, spaceAfter=8)
        ))

        SIDE_W  = 5.0 * cm
        IMG_COL = (W - SIDE_W) / 3       # exactly fills remaining width
        IMG_W   = IMG_COL - 0.3 * cm     # image slightly smaller than cell
        IMG_H   = IMG_W                  # keep square

        for entry, s in csam_entries:
            # Section label: Test — Sample ID
            story.append(Paragraph(
                f"{entry['test']['name']}  —  {s['id']}",
                sub_h
            ))

            cells = []
            captions = []
            for label, key in [("Before PC", "img_bpc"),
                                ("After PC",  "img_apc"),
                                ("After Test","img_atst")]:
                uri = s.get(key, "")
                if uri:
                    img = _b64_to_rl_image(uri, IMG_W, IMG_H)
                else:
                    img = Paragraph("No image", img_cap)
                cells.append(img or Paragraph("—", img_cap))
                captions.append(Paragraph(label, img_cap))

            img_tbl = Table(
                [cells, captions],
                colWidths=[IMG_COL, IMG_COL, IMG_COL],
            )
            img_tbl.setStyle(TableStyle([
                ("ALIGN",         (0,0), (-1,-1), "CENTER"),
                ("VALIGN",        (0,0), (-1,-1), "TOP"),
                ("TOPPADDING",    (0,0), (-1,-1), 4),
                ("BOTTOMPADDING", (0,0), (-1,-1), 2),
                ("LEFTPADDING",   (0,0), (-1,-1), 4),
                ("RIGHTPADDING",  (0,0), (-1,-1), 4),
                ("GRID",          (0,0), (-1,-1), 0.3, colors.HexColor("#dee2e6")),
            ]))

            # Metadata sidebar
            csam_ok = s.get("csam_status","") == "Pass"
            status_color = colors.HexColor("#198754") if csam_ok else colors.HexColor("#dc3545")
            side_data = [
                [Paragraph("CSAM Status", S("SL", fontSize=7, textColor=colors.grey)),
                 Paragraph(s.get("csam_status","—"),
                           S("SV", fontSize=8, fontName="Helvetica-Bold",
                             textColor=status_color))],
                [Paragraph("Before PC",   S("SL", fontSize=7, textColor=colors.grey)),
                 Paragraph(f"{s['csam_bpc']:.1f}%" if s.get("csam_bpc") is not None else "—",
                           S("SV2", fontSize=8))],
                [Paragraph("After PC",    S("SL2", fontSize=7, textColor=colors.grey)),
                 Paragraph(f"{s['csam_apc']:.1f}%" if s.get("csam_apc") is not None else "—",
                           S("SV3", fontSize=8))],
                [Paragraph("After Test",  S("SL3", fontSize=7, textColor=colors.grey)),
                 Paragraph(f"{s['csam_atst']:.1f}%" if s.get("csam_atst") is not None else "—",
                           S("SV4", fontSize=8))],
            ]
            side_tbl = Table(side_data, colWidths=[2.2*cm, 2.8*cm])
            side_tbl.setStyle(TableStyle([
                ("TOPPADDING",    (0,0),(-1,-1), 3),
                ("BOTTOMPADDING", (0,0),(-1,-1), 3),
                ("LEFTPADDING",   (0,0),(-1,-1), 4),
                ("GRID",          (0,0),(-1,-1), 0.3, colors.HexColor("#dee2e6")),
                ("BACKGROUND",    (0,0),(-1,-1), colors.HexColor("#f8f9fa")),
            ]))

            row_tbl = Table([[img_tbl, side_tbl]],
                             colWidths=[3*IMG_COL, SIDE_W])
            row_tbl.setStyle(TableStyle([
                ("VALIGN", (0,0),(-1,-1), "TOP"),
                ("LEFTPADDING",  (0,0),(-1,-1), 0),
                ("RIGHTPADDING", (0,0),(-1,-1), 0),
                ("TOPPADDING",   (0,0),(-1,-1), 0),
                ("BOTTOMPADDING",(0,0),(-1,-1), 0),
            ]))
            story.append(row_tbl)
            story.append(Spacer(1, 0.6*cm))

    # ── 9. Footer ─────────────────────────────────────────────────────────────
    story.append(Spacer(1, 0.5*cm))
    story.append(HRFlowable(width=W, color=colors.HexColor("#dee2e6")))
    story.append(Spacer(1, 0.2*cm))
    story.append(Paragraph(
        f"Package Reliability Qualification Suite — {r['date']}",
        S("FTR", fontSize=8, textColor=colors.grey, alignment=TA_CENTER)
    ))

    doc.build(story)
    buf.seek(0)
    return buf.getvalue()


# ── Project handlers ──────────────────────────────────────────────────────────

def _fmt_dt(iso: str) -> str:
    """Format ISO datetime string to readable form."""
    try:
        return datetime.fromisoformat(iso).strftime("%b %d, %Y")
    except Exception:
        return iso or "—"

class ProjectListHandler(Base):
    def get(self):
        projects = _db.list_projects()
        if projects:
            rows = ""
            for p in projects:
                status_color = {"active": "success", "complete": "info", "archived": "secondary"}.get(p["status"], "secondary")
                rows += f"""
                <tr>
                  <td><a href="/projects/{p['id']}" class="fw-semibold text-decoration-none"
                         style="color:var(--df-black)">{p['name']}</a></td>
                  <td class="text-muted" style="font-size:.83rem">{p['description'] or '—'}</td>
                  <td><span class="badge" style="font-size:.72rem;background:var(--df-bg);color:var(--df-mid);border:1px solid var(--df-border)">
                    {'TTV' if p['part_type']=='ttv' else 'Active'}</span></td>
                  <td><span class="badge bg-{status_color}">{p['status'].capitalize()}</span></td>
                  <td class="text-muted" style="font-size:.82rem">{_fmt_dt(p['updated_at'])}</td>
                  <td class="text-muted" style="font-size:.82rem">{_fmt_dt(p['created_at'])}</td>
                  <td>
                    <a href="/projects/{p['id']}" class="btn btn-sm btn-outline-secondary">Open</a>
                    <button class="btn btn-sm btn-outline-secondary ms-1"
                            style="color:var(--df-grey)"
                            onclick="confirmDelete({p['id']}, '{p['name'].replace(chr(39), chr(92)+chr(39))}')">
                      Delete
                    </button>
                  </td>
                </tr>"""
            table = f"""
            <div class="card shadow-sm">
              <table class="table table-hover mb-0">
                <thead class="tbl-header">
                  <tr><th>Project</th><th>Description</th><th>Type</th><th>Status</th>
                      <th>Last Updated</th><th>Created</th><th></th></tr>
                </thead>
                <tbody>{rows}</tbody>
              </table>
            </div>"""
        else:
            table = """<div class="card text-center py-5 text-muted shadow-sm">
              <i class="bi bi-folder2-open fs-1 mb-3"></i>
              <p class="mb-0">No projects yet — create your first one below.</p>
            </div>"""

        body = f"""
        <!-- Delete-project confirmation modal -->
        <div class="modal fade" id="deleteModal" tabindex="-1">
          <div class="modal-dialog modal-dialog-centered">
            <div class="modal-content">
              <div class="modal-header border-0 pb-0">
                <h6 class="modal-title text-danger">
                  <i class="bi bi-exclamation-triangle me-2"></i>Delete Project
                </h6>
                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
              </div>
              <div class="modal-body pt-2">
                <p style="font-size:.9rem" class="mb-1">You are about to permanently delete:</p>
                <p id="del-project-name" class="fw-semibold mb-3" style="font-size:1rem"></p>
                <div class="alert alert-danger py-2 mb-0" style="font-size:.83rem">
                  This will delete all sample data, pass/fail records, schedule tasks, and reports
                  for this project. <strong>This cannot be undone.</strong>
                </div>
              </div>
              <div class="modal-footer">
                <button type="button" class="btn btn-sm btn-outline-secondary"
                        data-bs-dismiss="modal">Cancel</button>
                <form id="deleteForm" method="post" class="d-inline">
                  <button type="submit" class="btn btn-sm btn-danger">
                    Yes, delete permanently
                  </button>
                </form>
              </div>
            </div>
          </div>
        </div>

        <div class="d-flex align-items-center justify-content-between mb-4">
          <h4 class="mb-0" style="font-weight:300">Projects</h4>
        </div>
        {table}
        <div class="card mt-4 shadow-sm">
          <div class="card-df"><h6 class="mb-0">New Project</h6></div>
          <div class="card-body p-4">
            <form method="post" action="/projects/new">
              <div class="row g-3">
                <div class="col-md-4">
                  <label class="form-label">Project Name <span class="text-danger">*</span></label>
                  <input type="text" class="form-control" name="name" required placeholder="e.g. TTV Phase 1 — Cu TCB">
                </div>
                <div class="col-md-4">
                  <label class="form-label">Description</label>
                  <input type="text" class="form-control" name="description" placeholder="Optional short description">
                </div>
                <div class="col-md-2">
                  <label class="form-label">Device Type</label>
                  <select class="form-select" name="part_type">
                    <option value="ttv">TTV</option>
                    <option value="active">Active Device</option>
                  </select>
                </div>
                <div class="col-md-2 d-flex align-items-end">
                  <button type="submit" class="btn btn-primary w-100">
                    <i class="bi bi-plus-lg me-1"></i>Create Project
                  </button>
                </div>
              </div>
            </form>
          </div>
        </div>
        <script>
        function confirmDelete(pid, name) {{
          document.getElementById('del-project-name').textContent = name;
          document.getElementById('deleteForm').action = '/projects/' + pid + '/delete';
          bootstrap.Modal.getOrCreateInstance(document.getElementById('deleteModal')).show();
        }}
        </script>"""
        self.emit(body, "Projects", active="projects")

    def post(self):
        # Redirect to /projects/new handler
        self.redirect("/projects/new")

class ProjectNewHandler(Base):
    def post(self):
        name        = self.get_argument("name", "").strip()
        description = self.get_argument("description", "").strip()
        part_type   = self.get_argument("part_type", "ttv")
        if not name:
            self.redirect("/projects")
            return
        pid = _db.create_project(name, description, part_type)
        # Sync part_type to session for consistency
        _, s = self.sess()
        s["part_type"] = part_type
        self.redirect(f"/projects/{pid}")

class ProjectDeleteHandler(Base):
    def post(self, pid):
        _db.delete_project(int(pid))
        self.redirect("/projects")

class ProjectDetailHandler(Base):
    def get(self, pid):
        pid = int(pid)
        p   = _db.get_project(pid)
        if not p:
            self.send_error(404); return
        meta  = _db.get_meta(pid)
        ss    = _db.get_sample_size(pid)
        pf    = _db.get_pass_fail(pid)
        samps = _db.get_samples(pid)

        # Quick stats
        n_tests_with_data = len([k for k, v in samps.items() if v])
        pf_verdict = ""
        if pf.get("n"):
            from jedec_calc import pass_fail as _pff, demonstrated_reliability
            try:
                n_v  = int(pf["n"])
                r_req = 1.0 - float(pf["ltpd_pct"]) / 100.0
                passed, r_demo = _pff(n_v, int(pf["failures"]), float(pf["confidence"]), r_req)
                pf_verdict = f'<span class="badge bg-{"success" if passed else "danger"} ms-2">{"PASS" if passed else "FAIL"}</span>'
            except Exception:
                pass

        status_opts = "".join(
            f'<option value="{v}" {"selected" if p["status"]==v else ""}>{l}</option>'
            for v, l in [("active","Active"),("complete","Complete"),("archived","Archived")]
        )

        meta_device  = meta.get("device_name","")
        meta_pkg     = meta.get("device_pkg","")
        meta_bond    = meta.get("bond_type","")
        meta_eng     = meta.get("engineer","")
        meta_lot     = meta.get("lot_id","")
        meta_notes   = meta.get("notes","")

        body = f"""
        <div class="row g-4">
          <div class="col-lg-8">
            <div class="card shadow-sm mb-4">
              <div class="card-df d-flex align-items-center justify-content-between">
                <h6 class="mb-0">Project Details</h6>
                <button class="btn btn-sm btn-outline-secondary" onclick="document.getElementById('edit-form').classList.toggle('d-none')">
                  <i class="bi bi-pencil me-1"></i>Edit
                </button>
              </div>
              <div class="card-body p-4">
                <!-- Read-only view -->
                <div id="view-details">
                  <div class="row g-3" style="font-size:.87rem">
                    <div class="col-sm-4"><span class="text-muted d-block">Device / Part</span><strong>{meta_device or '—'}</strong></div>
                    <div class="col-sm-4"><span class="text-muted d-block">Package</span><strong>{meta_pkg or '—'}</strong></div>
                    <div class="col-sm-4"><span class="text-muted d-block">Bond Type</span><strong>{meta_bond or '—'}</strong></div>
                    <div class="col-sm-4"><span class="text-muted d-block">Engineer</span><strong>{meta_eng or '—'}</strong></div>
                    <div class="col-sm-4"><span class="text-muted d-block">Lot / Wafer ID</span><strong>{meta_lot or '—'}</strong></div>
                    <div class="col-sm-4"><span class="text-muted d-block">Device Type</span>
                      <strong>{'TTV' if p['part_type']=='ttv' else 'Active Device'}</strong></div>
                    {'<div class="col-12"><span class="text-muted d-block">Notes</span><span>' + meta_notes + '</span></div>' if meta_notes else ''}
                  </div>
                </div>
                <!-- Edit form (hidden by default) -->
                <form id="edit-form" class="d-none" method="post" action="/projects/{pid}/meta">
                  <div class="row g-3">
                    <div class="col-md-4">
                      <label class="form-label">Device / Part</label>
                      <input type="text" class="form-control" name="device_name" value="{meta_device}">
                    </div>
                    <div class="col-md-4">
                      <label class="form-label">Package</label>
                      <input type="text" class="form-control" name="device_pkg" value="{meta_pkg}">
                    </div>
                    <div class="col-md-4">
                      <label class="form-label">Bond Type</label>
                      <input type="text" class="form-control" name="bond_type" value="{meta_bond}">
                    </div>
                    <div class="col-md-4">
                      <label class="form-label">Engineer</label>
                      <input type="text" class="form-control" name="engineer" value="{meta_eng}">
                    </div>
                    <div class="col-md-4">
                      <label class="form-label">Lot / Wafer ID</label>
                      <input type="text" class="form-control" name="lot_id" value="{meta_lot}">
                    </div>
                    <div class="col-md-4">
                      <label class="form-label">Status</label>
                      <select class="form-select" name="status">{status_opts}</select>
                    </div>
                    <div class="col-12">
                      <label class="form-label">Notes</label>
                      <textarea class="form-control" name="notes" rows="2">{meta_notes}</textarea>
                    </div>
                    <div class="col-12 d-flex gap-2">
                      <button type="submit" class="btn btn-primary">Save</button>
                      <button type="button" class="btn btn-outline-secondary"
                              onclick="document.getElementById('edit-form').classList.add('d-none')">Cancel</button>
                    </div>
                  </div>
                </form>
              </div>
            </div>
          </div>

          <div class="col-lg-4">
            <div class="card shadow-sm mb-3">
              <div class="card-df"><h6 class="mb-0">Quick Actions</h6></div>
              <div class="list-group list-group-flush">
                <a href="/projects/{pid}/sample-size" class="list-group-item list-group-item-action d-flex align-items-center gap-3 py-3">
                  <i class="bi bi-calculator fs-5 text-muted"></i>
                  <div><div class="fw-semibold" style="font-size:.87rem">Sample Size Planner</div>
                  <div class="text-muted" style="font-size:.78rem">LTPD={ss['ltpd']}%, C={ss['failures']} failures</div></div>
                  <i class="bi bi-chevron-right ms-auto text-muted"></i>
                </a>
                <a href="/projects/{pid}/report" class="list-group-item list-group-item-action d-flex align-items-center gap-3 py-3">
                  <i class="bi bi-file-earmark-text fs-5 text-muted"></i>
                  <div><div class="fw-semibold" style="font-size:.87rem">Report Generation</div>
                  <div class="text-muted" style="font-size:.78rem">{n_tests_with_data} test{'s' if n_tests_with_data!=1 else ''} with sample data</div></div>
                  <i class="bi bi-chevron-right ms-auto text-muted"></i>
                </a>
                <a href="/projects/{pid}/csam" class="list-group-item list-group-item-action d-flex align-items-center gap-3 py-3">
                  <i class="bi bi-images fs-5 text-muted"></i>
                  <div><div class="fw-semibold" style="font-size:.87rem">CSAM Gallery</div>
                  <div class="text-muted" style="font-size:.78rem">Browse images by test and sample</div></div>
                  <i class="bi bi-chevron-right ms-auto text-muted"></i>
                </a>
                <a href="/projects/{pid}/tracker" class="list-group-item list-group-item-action d-flex align-items-center gap-3 py-3">
                  <i class="bi bi-calendar3 fs-5 text-muted"></i>
                  <div><div class="fw-semibold" style="font-size:.87rem">Schedule</div>
                  <div class="text-muted" style="font-size:.78rem">GANTT chart &amp; task timeline</div></div>
                  <i class="bi bi-chevron-right ms-auto text-muted"></i>
                </a>
              </div>
            </div>
          </div>
        </div>"""
        self.emit(body, p["name"], active="projects", project=p, active_sub="overview")

    def post(self, pid):
        # POST to project root → redirect to projects list
        self.redirect("/projects")

class ProjectMetaHandler(Base):
    def post(self, pid):
        pid = int(pid)
        p   = _db.get_project(pid)
        if not p:
            self.redirect("/projects"); return
        _db.save_meta(pid,
            device_name = self.get_argument("device_name", ""),
            device_pkg  = self.get_argument("device_pkg",  ""),
            bond_type   = self.get_argument("bond_type",   ""),
            engineer    = self.get_argument("engineer",    ""),
            lot_id      = self.get_argument("lot_id",      ""),
            notes       = self.get_argument("notes",       ""),
        )
        status = self.get_argument("status", p["status"])
        _db.update_project(pid, status=status)
        self.redirect(f"/projects/{pid}")

# ── Project-scoped: Sample Size ───────────────────────────────────────────────

class ProjectSampleSizeHandler(Base):
    def _get_project_or_404(self, pid):
        p = _db.get_project(int(pid))
        if not p:
            self.send_error(404)
        return p

    def get(self, pid):
        p = self._get_project_or_404(pid)
        if not p: return
        saved = _db.get_sample_size(int(pid))
        self._render(p, k=saved["failures"], ltpd=saved["ltpd"], confidence=saved.get("confidence", 0.90))

    def post(self, pid):
        p = self._get_project_or_404(pid)
        if not p: return
        action = self.get_argument("action", "planner")
        if action == "per_test":
            # Save per-test sample counts
            tests = applicable_tests(p["part_type"])
            existing = _db.get_samples(int(pid))
            for key in tests:
                val = self.get_argument(f"n_{key}", "")
                if val.strip().isdigit():
                    existing[key] = int(val.strip())
            _db.save_samples(int(pid), existing)
            self._render(p)
            return
        try:
            k          = int(self.get_argument("failures", "0"))
            ltpd       = float(self.get_argument("ltpd", "5"))
            confidence = float(self.get_argument("confidence", "0.90"))
            if k < 0: raise ValueError("Acceptance number C must be ≥ 0")
            if not (0.01 <= ltpd <= 99.99): raise ValueError("LTPD must be between 0.01% and 99.99%")
            if not (0.50 <= confidence <= 0.9999): raise ValueError("Confidence must be between 0.50 and 0.9999")
            _db.save_sample_size(int(pid), ltpd, k, confidence)
            n_jesd47 = min_sample_size_ltpd(ltpd, k)
            r_equiv  = 1.0 - ltpd / 100.0
            n_exact  = min_sample_size(r_equiv, confidence, k)
            self._render(p, k=k, ltpd=ltpd, confidence=confidence, n_jesd47=n_jesd47, n_exact=n_exact)
        except Exception as e:
            self._render(p, error=str(e))

    def _render(self, p, k=0, ltpd=5.0, confidence=0.90, n_jesd47=None, n_exact=None, error=None):
        pid   = p["id"]
        tests = applicable_tests(p["part_type"])
        saved_counts = _db.get_samples(int(pid))

        # ── Table A ────────────────────────────────────────────────────────
        ltpd_cols = TABLE_A_LTPD
        nearest_ltpd = min(ltpd_cols, key=lambda l: abs(l - ltpd)) if n_jesd47 is not None else None
        hdr_cells = "".join(
            f'<th style="padding:3px 5px;font-size:.7rem;text-align:center;white-space:nowrap;'
            f'{"background:#dbeafe;font-weight:700;" if l == nearest_ltpd else ""}">'
            f'{l}%</th>'
            for l in ltpd_cols
        )
        tbl_rows = ""
        for c_val, row in TABLE_A.items():
            row_bg = ' style="background:#eff6ff"' if (n_jesd47 is not None and c_val == k) else ""
            cells = ""
            for ci, n_val in enumerate(row):
                is_sel = (n_jesd47 is not None and c_val == k and ltpd_cols[ci] == nearest_ltpd)
                td_s = ' style="font-weight:700;color:#1d4ed8;padding:3px 5px;font-size:.75rem;text-align:center"' if is_sel else ' style="padding:3px 5px;font-size:.75rem;text-align:center"'
                cells += f"<td{td_s}>{n_val}</td>"
            tbl_rows += f'<tr{row_bg}><td style="padding:3px 5px;font-size:.75rem;font-weight:600">{c_val}</td>{cells}</tr>'

        table_a_html = f"""
        <div class="card mb-3" style="border:1px solid var(--df-border)">
          <div class="card-df d-flex align-items-center gap-2">
            <h6 class="mb-0" style="font-size:.82rem">Table A — JESD47I §3.8</h6>
            <span class="text-white-50" style="font-size:.7rem">Max % defective at 90% confidence</span>
          </div>
          <div class="table-responsive">
            <table class="table table-sm table-bordered mb-0" style="font-size:.75rem">
              <thead class="tbl-header">
                <tr>
                  <th style="padding:3px 5px;font-size:.7rem;white-space:nowrap">C</th>
                  {hdr_cells}
                </tr>
              </thead>
              <tbody>{tbl_rows}</tbody>
            </table>
          </div>
          <div class="card-footer text-muted py-1 px-2" style="font-size:.68rem">
            Highlighted = selected C &amp; LTPD. &ensp;
            <a href="{SPEC_URLS['jesd47']}" target="_blank" style="font-size:.68rem">
              <i class="bi bi-file-earmark-pdf me-1"></i>JESD47I PDF
            </a>
          </div>
        </div>"""

        # ── Result card ────────────────────────────────────────────────────
        if error:
            result_html = f'<div class="alert alert-danger" style="font-size:.82rem"><i class="bi bi-exclamation-triangle me-2"></i>{error}</div>'
        elif n_jesd47 is not None:
            r_equiv = 1.0 - ltpd / 100.0
            result_html = f"""
            <div class="card mb-3" style="border:1px solid var(--df-border)">
              <div class="card-body text-center py-3">
                <div class="text-muted" style="font-size:.73rem">Min Sample Size (JESD47I)</div>
                <div style="font-size:2rem;font-weight:700;color:var(--df-navy);line-height:1.2">{n_jesd47}</div>
                <div class="text-muted mt-1" style="font-size:.72rem">
                  ≤<strong>{ltpd}%</strong> defective · 90% CL · C={k}
                </div>
                <div class="text-muted" style="font-size:.7rem">
                  Exact χ²: <strong>{n_exact}</strong>
                  {"&ensp;<span class='badge bg-secondary' style='font-size:.65rem'>same</span>" if n_exact == n_jesd47 else f"&ensp;<span style='color:#6b7280'>({'+' if n_jesd47>n_exact else ''}{n_jesd47-n_exact} vs exact)</span>"}
                </div>
              </div>
            </div>"""
        else:
            result_html = ""

        # ── Per-test allocation ────────────────────────────────────────────
        alloc_rows = ""
        for key, t in tests.items():
            count = saved_counts.get(key, "")
            if isinstance(count, list):
                count = len(count) if count else ""
            alloc_rows += (
                f'<tr>'
                f'<td style="padding:5px 8px;font-size:.78rem;font-weight:500">{t["name"]}</td>'
                f'<td style="padding:5px 8px;font-size:.72rem;color:var(--df-grey)">{t.get("standard","")}</td>'
                f'<td style="padding:5px 8px">'
                f'<input type="number" name="n_{key}" value="{count}" min="0" '
                f'class="form-control form-control-sm" style="width:72px" placeholder="—">'
                f'</td>'
                f'</tr>'
            )

        per_test_panel = f"""
        <div class="card" style="border:1px solid var(--df-border)">
          <div class="card-df d-flex justify-content-between align-items-center">
            <h6 class="mb-0" style="font-size:.82rem">Sample Allocation per Test</h6>
            <span style="font-size:.7rem;color:rgba(255,255,255,.6)">Shown on Schedule</span>
          </div>
          <div class="card-body p-0">
            <form method="post" action="/projects/{pid}/sample-size">
              <input type="hidden" name="action" value="per_test">
              <table class="table table-sm mb-0">
                <thead class="tbl-header">
                  <tr>
                    <th style="padding:5px 8px;font-size:.72rem">Test</th>
                    <th style="padding:5px 8px;font-size:.72rem">Std</th>
                    <th style="padding:5px 8px;font-size:.72rem">n</th>
                  </tr>
                </thead>
                <tbody>{alloc_rows}</tbody>
              </table>
              <div class="p-2 border-top">
                <button type="submit" class="btn btn-sm"
                  style="background:var(--df-accent);color:#fff;border:none;font-size:.78rem">
                  Save Sample Counts
                </button>
              </div>
            </form>
          </div>
        </div>"""

        body = f"""
        <h5 class="mb-3" style="font-weight:300">Sample Size Planner — {p['name']}</h5>
        <div class="row g-3">
          <!-- Left: Parameters + Result -->
          <div class="col-xl-3 col-lg-4">
            <div class="card" style="border:1px solid var(--df-border)">
              <div class="card-df"><h6 class="mb-0" style="font-size:.83rem">Parameters</h6></div>
              <div class="card-body p-3">
                <p class="text-muted mb-3" style="font-size:.76rem">
                  Per <strong>JESD47I §3.8</strong> — minimum units to demonstrate
                  a maximum defect rate at 90% confidence.
                </p>
                <form method="post" action="/projects/{pid}/sample-size">
                  <div class="mb-3">
                    <label class="form-label" style="font-size:.8rem">LTPD — Max % Defective</label>
                    <div class="input-group input-group-sm">
                      <input type="number" class="form-control" name="ltpd"
                             value="{ltpd}" min="0.01" max="99.99" step="0.01" required>
                      <span class="input-group-text">%</span>
                    </div>
                    <div class="form-text" style="font-size:.7rem">Used for pass/fail in Reporting</div>
                  </div>
                  <div class="mb-3">
                    <label class="form-label" style="font-size:.8rem">Confidence Level</label>
                    <div class="input-group input-group-sm">
                      <input type="number" class="form-control" name="confidence"
                             value="{confidence}" min="0.50" max="0.9999" step="0.01" required>
                      <span class="input-group-text">e.g. 0.90</span>
                    </div>
                    <div class="form-text" style="font-size:.7rem">Used for pass/fail in Reporting</div>
                  </div>
                  <div class="mb-3">
                    <label class="form-label" style="font-size:.8rem">Acceptance Number (C)</label>
                    <input type="number" class="form-control form-control-sm" name="failures"
                           value="{k}" min="0" step="1" required>
                    <div class="form-text" style="font-size:.7rem">0 = zero-failure plan</div>
                  </div>
                  <button type="submit" class="btn btn-sm btn-primary w-100">Calculate →</button>
                </form>
                <hr class="my-3">
                <p class="text-muted mb-0" style="font-size:.7rem">
                  LTPD 5% ≡ R = 95%. Confidence and LTPD
                  are applied when evaluating pass/fail in the Reporting tab.
                </p>
              </div>
            </div>
            {result_html}
          </div>
          <!-- Middle: Table A -->
          <div class="col-xl-5 col-lg-4">
            {table_a_html}
          </div>
          <!-- Right: Sample Allocation -->
          <div class="col-xl-4 col-lg-4">
            {per_test_panel}
          </div>
        </div>"""
        self.emit(body, f"Planner — {p['name']}", active="projects",
                  project=p, active_sub="sample-size")

# ── Project-scoped: Pass / Fail ───────────────────────────────────────────────

class ProjectPassFailHandler(Base):
    def _get_project_or_404(self, pid):
        p = _db.get_project(int(pid))
        if not p:
            self.send_error(404)
        return p

    def get(self, pid):
        p = self._get_project_or_404(pid)
        if not p: return
        saved = _db.get_pass_fail(int(pid))
        n_val = saved.get("n", "")
        try:
            n_val = int(n_val) if n_val else ""
        except Exception:
            n_val = ""
        self._render(p, n=n_val, k=saved.get("failures",0),
                     c=saved.get("confidence",0.90), ltpd_inp=saved.get("ltpd_pct",5.0))

    def post(self, pid):
        p = self._get_project_or_404(pid)
        if not p: return
        try:
            n        = int(self.get_argument("n", ""))
            k        = int(self.get_argument("failures", "0"))
            c        = float(self.get_argument("confidence", "0.90"))
            ltpd_inp = float(self.get_argument("ltpd_pct", "5.0"))
            if n < 1:     raise ValueError("Need at least 1 sample")
            if k > n:     raise ValueError("Failures cannot exceed samples tested")
            if not (0.50 <= c <= 0.9999): raise ValueError("Confidence must be 0.50–0.9999")
            if not (0.01 <= ltpd_inp <= 99.99): raise ValueError("Defective Rate must be 0.01–99.99%")
            _db.save_pass_fail(int(pid), n, k, c, ltpd_inp)
            r_req = 1.0 - ltpd_inp / 100.0
            passed, r_demo = _pf(n, k, c, r_req)
            sens = [(f, demonstrated_reliability(n, f, c)) for f in range(min(n+1, 9))]
            self._render(p, n=n, k=k, c=c, ltpd_inp=ltpd_inp,
                         passed=passed, r_demo=r_demo, sens=sens)
        except (ValueError, TypeError) as e:
            self._render(p, error=str(e))

    def _render(self, p, n="", k=0, c=0.90, ltpd_inp=5.0,
                passed=None, r_demo=None, sens=None, error=None):
        captured = {}
        def _capture(body, title="", active="", project=None, active_sub=""):
            captured["body"] = body
        orig_emit = self.emit
        self.emit = _capture
        PassFailHandler._render(self, n=n, k=k, c=c, ltpd_inp=ltpd_inp,
                                passed=passed, r_demo=r_demo, sens=sens, error=error)
        self.emit = orig_emit
        body = captured.get("body", "")
        self.emit(body, f"Pass/Fail — {p['name']}", active="projects",
                  project=p, active_sub="pass-fail")

# ── Project-scoped: Qual Report ───────────────────────────────────────────────

class ProjectReportHandler(ReportHandler):
    def _get_project_or_404(self, pid):
        p = _db.get_project(int(pid))
        if not p:
            self.send_error(404)
        return p

    def get(self, pid):
        p = self._get_project_or_404(pid)
        if not p: return
        _, s = self.sess()
        s["part_type"] = p["part_type"]
        # Inject saved planner sample counts so _render_form can use them as n defaults
        self._saved_n = _db.get_samples(int(pid))
        # Inject project LTPD + confidence so pass/fail in the form uses project settings
        _qual = _db.get_sample_size(int(pid))
        self._qual_ltpd       = _qual.get("ltpd", 5.0)
        self._qual_confidence = _qual.get("confidence", 0.90)
        # Make the form POST back to the project-scoped URL (not the standalone /report)
        self._form_action = f"/projects/{pid}/report"
        captured = {}
        def _capture(body, title="", active="", project=None, active_sub=""):
            captured["body"] = body
        orig_emit = self.emit
        self.emit = _capture
        ReportHandler.get(self)
        self.emit = orig_emit
        body = captured.get("body", "")
        self.emit(body, f"Qual Report — {p['name']}", active="projects",
                  project=p, active_sub="report")

    def post(self, pid):
        p = self._get_project_or_404(pid)
        if not p: return
        _, s = self.sess()
        s["part_type"] = p["part_type"]
        # Inject project LTPD + confidence so ReportHandler.post uses them for pass/fail
        _qual = _db.get_sample_size(int(pid))
        self._qual_ltpd       = _qual.get("ltpd", 5.0)
        self._qual_confidence = _qual.get("confidence", 0.90)
        # Delegate to existing ReportHandler.post which builds report dict and stores in session
        captured = {}
        def _capture(body, title="", active="", project=None, active_sub=""):
            captured["body"] = body
        orig_emit = self.emit
        self.emit = _capture
        ReportHandler.post(self)
        self.emit = orig_emit
        # Save whatever samples were parsed back to DB
        report = s.get("last_report")
        if report and isinstance(report, dict):
            samples = report.get("samples", {})
            if samples:
                _db.save_samples(int(pid), samples)
                # Auto-save CSAM images into the gallery
                _img_stage_map = [
                    ("img_bpc",  "pre_cond"),
                    ("img_apc",  "post_cond"),
                    ("img_atst", "post_test"),
                ]
                for test_key, sample_list in samples.items():
                    if not isinstance(sample_list, list):
                        continue
                    for sample in sample_list:
                        sample_id = sample.get("id", "")
                        for field, stage in _img_stage_map:
                            img_data = sample.get(field, "")
                            if img_data:
                                _db.upsert_csam_image(
                                    int(pid), sample_id, test_key, stage, img_data
                                )
        body = captured.get("body", "")
        self.emit(body, f"Qual Report — {p['name']}", active="projects",
                  project=p, active_sub="report")

class ProjectReportPdfHandler(Base):
    def get(self, pid):
        p = _db.get_project(int(pid))
        if not p:
            self.send_error(404); return
        _, s = self.sess()
        s["part_type"] = p["part_type"]
        # Delegate entirely to existing PDF handler
        ReportPdfHandler.get(self)


# ── Project-scoped: CSAM Image Repository ─────────────────────────────────────

_CSAM_TEST_LABELS = {
    "uhast": "uHAST", "tc": "TC", "tshock": "T-Shock", "mshock": "M-Shock",
    "vib": "Vibration", "pc": "Power Cycling", "hts": "HTS",
    "shadow_moire": "Shadow Moiré", "htol": "HTOL", "elfr": "ELFR",
    "thb": "THB", "esd_cdm": "ESD CDM", "esd_hbm": "ESD HBM",
    "latchup": "Latch-up", "ptc": "PTC",
}
_CSAM_STAGES = [
    ("pre_bond",  "Pre-Bond"),
    ("post_bond", "Post-Bond"),
    ("post_assy", "Post-Assy"),
    ("pre_cond",  "Pre-Conditioning"),
    ("post_cond", "Post-Conditioning"),
    ("post_test", "Post-Test"),
    ("other",     "Other"),
]

class ProjectCsamHandler(Base):
    def _get_or_404(self, pid):
        p = _db.get_project(int(pid))
        if not p:
            self.send_error(404)
        return p

    def get(self, pid):
        p = self._get_or_404(pid)
        if not p: return
        images = _db.list_csam_images(int(pid))

        # Group by test_key → sample_id → stage order
        from collections import OrderedDict
        groups = OrderedDict()  # test_key → {sample_id → [img, ...]}
        for img in images:
            tk = img["test_key"] or "—"
            si = img["sample_id"] or "—"
            if tk not in groups:
                groups[tk] = OrderedDict()
            if si not in groups[tk]:
                groups[tk][si] = []
            groups[tk][si].append(img)

        _stage_map = dict(_CSAM_STAGES)
        gallery_html = ""
        if not images:
            gallery_html = (
                '<div class="text-center text-muted py-5" style="font-size:.9rem">'
                '<i class="bi bi-image fs-2 d-block mb-2"></i>'
                '<div>No images yet.</div>'
                '<div style="font-size:.8rem;margin-top:.5rem">Images appear here automatically '
                'when you upload them in the Reporting section.</div></div>'
            )
        else:
            for tk, samples in groups.items():
                tname = _CSAM_TEST_LABELS.get(tk, tk)
                gallery_html += (
                    f'<h6 class="mt-4 mb-2" style="font-size:.83rem;font-weight:700;'
                    f'text-transform:uppercase;letter-spacing:.05em;color:#4b5563">'
                    f'{tname}</h6>'
                )
                for si, imgs in samples.items():
                    gallery_html += (
                        f'<div style="font-size:.78rem;font-weight:600;color:#374151;'
                        f'margin-bottom:.4rem">{si}</div>'
                        f'<div class="row g-2 mb-3">'
                    )
                    for img in imgs:
                        stage_label = _stage_map.get(img["stage"], img["stage"] or "—")
                        iid = img["id"]
                        gallery_html += f"""
                        <div class="col-6 col-md-4 col-lg-3">
                          <div class="card shadow-sm h-100" style="border:1px solid #e5e7eb;overflow:hidden">
                            <a href="/projects/{pid}/csam/{iid}" target="_blank" class="d-block"
                               style="background:#f9fafb;text-align:center;padding:8px">
                              <img src="/projects/{pid}/csam/{iid}/thumb"
                                   style="max-height:130px;max-width:100%;object-fit:contain;border-radius:4px"
                                   alt="{stage_label}" loading="lazy">
                            </a>
                            <div class="card-body p-2">
                              <div style="font-size:.72rem;font-weight:600;color:#374151">{stage_label}</div>
                              {f'<div style="font-size:.68rem;color:#9ca3af;margin-top:2px">{img["notes"]}</div>' if img.get("notes") else ""}
                              <div style="font-size:.66rem;color:#d1d5db;margin-top:3px">{img["created_at"][:10]}</div>
                            </div>
                            <div class="card-footer p-2 d-flex gap-1">
                              <a href="/projects/{pid}/csam/{iid}" target="_blank"
                                 class="btn btn-sm flex-fill" style="font-size:.7rem;border:1px solid #e5e7eb">
                                <i class="bi bi-zoom-in"></i> View
                              </a>
                              <form method="post" action="/projects/{pid}/csam/{iid}/delete"
                                    class="d-inline flex-fill"
                                    onsubmit="return confirm('Delete this image?')">
                                <button class="btn btn-sm w-100" style="font-size:.7rem;
                                  border:1px solid #fca5a5;color:#dc2626;background:#fff">
                                  <i class="bi bi-trash"></i>
                                </button>
                              </form>
                            </div>
                          </div>
                        </div>"""
                    gallery_html += "</div>"

        body = f"""
        <h5 class="mb-1" style="font-weight:300">CSAM Gallery — {p['name']}</h5>
        <p class="text-muted mb-4" style="font-size:.83rem">
          Images are saved automatically when you submit the Reporting form.
          They are grouped by test, sample ID, and imaging stage.
        </p>

        <div class="card" style="border:1px solid var(--df-border)">
          <div class="card-df d-flex align-items-center justify-content-between">
            <h6 class="mb-0">Images</h6>
            <span class="badge bg-secondary">{len(images)} image{"s" if len(images)!=1 else ""}</span>
          </div>
          <div class="card-body p-3">
            {gallery_html}
          </div>
        </div>"""
        self.emit(body, f"CSAM Gallery — {p['name']}", active="projects",
                  project=p, active_sub="csam")


class ProjectCsamImageHandler(Base):
    """Serve a single CSAM image (full-size) or delete it."""

    def get(self, pid, iid):
        img = _db.get_csam_image(int(iid))
        if not img:
            self.send_error(404); return
        # Serve the image as a data URI in a minimal HTML page
        fname = img.get("filename") or "CSAM Image"
        data  = img.get("image_data", "")
        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.write(f"""<!doctype html><html><head><title>{fname}</title>
        <style>body{{margin:0;background:#111;display:flex;align-items:center;
        justify-content:center;min-height:100vh}}
        img{{max-width:98vw;max-height:98vh;object-fit:contain}}</style>
        </head><body><img src="{data}" alt="{fname}"></body></html>""")


class ProjectCsamThumbHandler(Base):
    """Serve thumbnail (same image, browser caches it)."""

    def get(self, pid, iid):
        img = _db.get_csam_image(int(iid))
        if not img:
            self.send_error(404); return
        data = img.get("image_data", "")
        # Extract mime and raw bytes from data URI
        if data.startswith("data:"):
            try:
                header, b64data = data.split(",", 1)
                mime = header.split(";")[0].split(":")[1]
                raw  = base64.b64decode(b64data)
                self.set_header("Content-Type", mime)
                self.set_header("Cache-Control", "public, max-age=86400")
                self.write(raw)
                return
            except Exception:
                pass
        self.send_error(404)


class ProjectCsamDeleteHandler(Base):
    def post(self, pid, iid):
        _db.delete_csam_image(int(iid))
        self.redirect(f"/projects/{pid}/csam")


# ── Dynamic JEDEC qualification task template ─────────────────────────────────
# Durations are computed from sample counts stored in project_samples.
# Tasks run sequentially; durations round up to whole weeks (min 1).

def _compute_seeded_tasks(sample_counts: dict) -> list[dict]:
    """Build the 21-step qual plan with durations derived from sample counts.

    Steps 1-6 run sequentially (preparation phase).
    Steps 7-21 are stress+post pairs that all START at the same week (parallel),
    but within each pair the post-test step follows its stress test sequentially.
    """
    def n(key):
        v = sample_counts.get(key, 0)
        return max(1, int(v)) if v else 1

    total = sum(
        int(v) for v in sample_counts.values()
        if isinstance(v, (int, float)) and v > 0
    ) or 1

    def cw(days):
        return max(1, math.ceil(days / 7))

    # ── Phase 1: sequential preparation steps ─────────────────────────────
    prep = [
        ("SCD Surface Prep",
            "Preparation", "", 2),
        ("SCD Bonding + CSAM",
            "Preparation", "", cw(total / 4)),   # 1 day per 4 samples
        ("TTV Assy",
            "Preparation", "", cw(total)),        # 1 day per sample
        ("CSAM",
            "Preparation", "", cw(total / 20)),
        ("TTV Calibration",
            "Preparation", "", cw(total / 4)),   # 1 day per 4 samples
        ("Func + Thermal Test",
            "Preparation", "", cw(total / 5)),   # 1 day per 5 samples
        ("Preconditioning + CSAM",
            "Stress", "", 2),
    ]

    result, sw = [], 1
    for name, cat, key, dur in prep:
        result.append({"task_name": name, "category": cat, "test_key": key,
                        "start_week": sw, "duration": dur})
        sw += dur

    # ── Phase 2: stress + post-stress pairs (all pairs start same week) ───
    # Each pair: (stress_name, stress_key, stress_dur,
    #             post_csam_name, post_test_name, test_key, post_test_dur)
    # Post steps split: CSAM (1 wk) → Testing (1 day/sample → weeks)
    stress_start = sw
    pairs = [
        ("uHAST",          "uhast",  1,          "Post-uHAST CSAM",   "Post-uHAST Testing",   "uhast",  cw(n("uhast"))),
        ("TC",             "tc",     6,          "Post-TC CSAM",      "Post-TC Testing",      "tc",     cw(n("tc"))),
        ("T-Shock",        "tshock", 1,          "Post-T-Shock CSAM", "Post-T-Shock Testing", "tshock", cw(n("tshock"))),
        ("M-Shock",        "mshock", 1,          "Post-M-Shock CSAM", "Post-M-Shock Testing", "mshock", cw(n("mshock"))),
        ("Vibration",      "vib",    1,          "Post-Vib CSAM",     "Post-Vib Testing",     "vib",    cw(n("vib"))),
        ("Power Cycling",  "pc",     cw(n("pc")),"Post-PC CSAM",      "Post-PC Testing",      "pc",     cw(n("pc"))),
        ("HTS",            "hts",    6,          "Post-HTS CSAM",     "Post-HTS Testing",     "hts",    cw(n("hts"))),
        ("Shadow Moiré",   "shadow_moire", 1,    None,                None,                   "",       0),
    ]

    for (sname, skey, sdur, pcsam_name, ptest_name, tkey, ptest_dur) in pairs:
        result.append({"task_name": sname, "category": "Stress", "test_key": skey,
                        "start_week": stress_start, "duration": sdur})
        post_sw = stress_start + sdur
        if pcsam_name:
            result.append({"task_name": pcsam_name, "category": "Analysis", "test_key": tkey,
                            "start_week": post_sw, "duration": 1})
            post_sw += 1
        if ptest_name:
            result.append({"task_name": ptest_name, "category": "Analysis", "test_key": tkey,
                            "start_week": post_sw, "duration": ptest_dur})

    return result

_GANTT_STATUS_COLORS = {
    "not_started": ("#9ca3af", "#6b7280"),  # (bar bg, text)
    "in_progress":  ("#f59e0b", "#92400e"),
    "complete":     ("#22c55e", "#14532d"),
    "blocked":      ("#ef4444", "#7f1d1d"),
    "na":           ("#e5e7eb", "#9ca3af"),
}
_GANTT_STATUS_LABELS = {
    "not_started": "Not Started",
    "in_progress":  "In Progress",
    "complete":     "Complete",
    "blocked":      "Blocked",
    "na":           "N/A",
}

# ── Project-scoped: GANTT Tracker ─────────────────────────────────────────────

class ProjectTrackerHandler(Base):
    def _get_project_or_404(self, pid):
        p = _db.get_project(int(pid))
        if not p:
            self.send_error(404)
        return p

    def get(self, pid):
        p = self._get_project_or_404(pid)
        if not p: return
        tasks      = _db.list_gantt_tasks(p["id"])
        has_hist   = _db.has_gantt_history(p["id"])
        body       = self._render_gantt(p, tasks, has_history=has_hist)
        self.emit(body, f"Schedule — {p['name']}", active="projects",
                  project=p, active_sub="tracker")

    def post(self, pid):
        p = self._get_project_or_404(pid)
        if not p: return
        action = self.get_argument("action", "")

        if action == "seed":
            sample_counts = _db.get_samples(p["id"])
            defaults = _compute_seeded_tasks(sample_counts)
            _db.bulk_add_gantt_tasks(p["id"], defaults)
        elif action == "clear":
            existing = _db.list_gantt_tasks(p["id"])
            if existing:
                _db.push_gantt_history(p["id"], "clear", existing)
            _db.clear_gantt_tasks(p["id"])
        elif action == "set_start":
            sd = self.get_argument("start_date", "").strip()
            if sd:
                _db.save_meta(p["id"], gantt_start_date=sd)
        elif action == "add":
            _db.add_gantt_task(
                p["id"],
                task_name  = self.get_argument("task_name", "New Task"),
                category   = self.get_argument("category", ""),
                start_week = int(self.get_argument("start_week", "1")),
                duration   = int(self.get_argument("duration", "1")),
                status     = self.get_argument("status", "not_started"),
            )
        elif action == "undo":
            entry = _db.pop_gantt_history(p["id"])
            if entry:
                atype    = entry["action_type"]
                snapshot = entry["snapshot"]
                if atype == "delete_task":
                    t = snapshot
                    _db.add_gantt_task(
                        p["id"], task_name=t["task_name"], category=t.get("category",""),
                        test_key=t.get("test_key",""),
                        start_week=t["start_week"], duration=t["duration"],
                        status=t["status"],
                    )
                elif atype == "edit_task":
                    t = snapshot
                    _db.update_gantt_task(
                        t["id"], p["id"],
                        task_name=t["task_name"], category=t.get("category",""),
                        test_key=t.get("test_key",""),
                        start_week=t["start_week"], duration=t["duration"],
                        status=t["status"],
                    )
                elif atype == "clear":
                    _db.clear_gantt_tasks(p["id"])
                    for t in snapshot:
                        _db.add_gantt_task(
                            p["id"], task_name=t["task_name"], category=t.get("category",""),
                            test_key=t.get("test_key",""),
                            start_week=t["start_week"], duration=t["duration"],
                            status=t["status"],
                        )
        elif action == "bulk_edit":
            import json as _json
            try:
                changes = _json.loads(self.get_argument("changes", "[]"))
                if changes:
                    # Push undo snapshot before making changes
                    existing = _db.list_gantt_tasks(p["id"])
                    _db.push_gantt_history(p["id"], "bulk_edit", existing)
                    for ch in changes:
                        if ch.get("duration", 0) > 0:
                            _db.update_gantt_task(
                                int(ch["id"]), p["id"],
                                start_week=int(ch["start_week"]),
                                duration=int(ch["duration"]),
                            )
            except Exception:
                pass  # malformed JSON or missing fields — silently ignore
        elif action == "reorder":
            import json as _json
            try:
                ordered_ids = _json.loads(self.get_argument("order", "[]"))
                if ordered_ids:
                    _db.reorder_gantt_tasks(p["id"], [int(i) for i in ordered_ids])
            except Exception:
                pass
            # Respond with 204 (no redirect — called via fetch)
            self.set_status(204)
            return
        self.redirect(f"/projects/{p['id']}/tracker")

    def _render_gantt(self, p: dict, tasks: list, has_history: bool = False) -> str:
        from datetime import date, timedelta
        pid = p["id"]

        # ── Calendar anchor ───────────────────────────────────────────────
        meta = _db.get_meta(pid)
        start_date_str = (meta.get("gantt_start_date") or "").strip()
        try:
            anchor = date.fromisoformat(start_date_str)
        except ValueError:
            anchor = date.today()
            # align to Monday of current week
            anchor = anchor - timedelta(days=anchor.weekday())

        today = date.today()
        # Which week number is today (1-indexed from anchor)?
        days_since = (today - anchor).days
        current_week = max(1, days_since // 7 + 1) if days_since >= 0 else None

        # ── Chart width ────────────────────────────────────────────────────
        if tasks:
            max_week = max(t["start_week"] + t["duration"] - 1 for t in tasks)
            n_weeks  = max(max_week + 2, 26)
        else:
            n_weeks = 26

        # ── Per-test sample counts ─────────────────────────────────────────
        sample_counts = _db.get_samples(pid)
        # Values may be plain ints (from planner) or lists (from report); normalise
        def _extract_n(raw):
            if isinstance(raw, list):
                return len(raw) if raw else 0
            try:
                v = int(raw)
                return v if v > 0 else 0
            except (TypeError, ValueError):
                return 0
        total_n = sum(_extract_n(v) for v in sample_counts.values()) if sample_counts else 0

        # ── Status badge helper ────────────────────────────────────────────
        def status_badge(s):
            bg, fg = _GANTT_STATUS_COLORS.get(s, ("#9ca3af", "#6b7280"))
            label  = _GANTT_STATUS_LABELS.get(s, s)
            return (f'<span style="background:{bg};color:{fg};padding:2px 8px;'
                    f'border-radius:4px;font-size:.72rem;font-weight:600">{label}</span>')

        # ── Category colors ────────────────────────────────────────────────
        _cat_colors = ["#3b82f6","#8b5cf6","#ec4899","#f97316","#14b8a6","#64748b"]
        cats      = list(dict.fromkeys(t["category"] for t in tasks if t["category"]))
        cat_color = {c: _cat_colors[i % len(_cat_colors)] for i, c in enumerate(cats)}

        # ── Week → date helper ────────────────────────────────────────────
        def week_date(w):
            return anchor + timedelta(weeks=w - 1)

        # ── Month/year header rows ─────────────────────────────────────────
        # Group consecutive weeks by month label "Jan 2026"
        month_spans = []  # list of (label, colspan)
        cur_label, span = None, 0
        for w in range(1, n_weeks + 1):
            d = week_date(w)
            lbl = d.strftime("%b %Y")
            if lbl == cur_label:
                span += 1
            else:
                if cur_label:
                    month_spans.append((cur_label, span))
                cur_label, span = lbl, 1
        if cur_label:
            month_spans.append((cur_label, span))

        month_headers = ""
        for lbl, cs in month_spans:
            month_headers += (
                f'<th colspan="{cs}" style="padding:3px 6px;text-align:center;'
                f'font-size:.72rem;color:#374151;font-weight:700;'
                f'border-left:2px solid #d1d5db;white-space:nowrap">{lbl}</th>'
            )

        # ── Week-number header row ─────────────────────────────────────────
        wk_headers = ""
        for w in range(1, n_weeks + 1):
            d     = week_date(w)
            label = f"W{d.isocalendar()[1]}"   # ISO week number
            is_now = (current_week == w)
            bg   = "#fef9c3" if is_now else "transparent"
            border = "border-left:2px solid #f59e0b;" if is_now else "border-left:1px solid #e5e7eb;"
            wk_headers += (
                f'<th style="min-width:34px;padding:3px 2px;text-align:center;'
                f'font-size:.68rem;color:{"#92400e" if is_now else "#9ca3af"};'
                f'font-weight:{"700" if is_now else "400"};background:{bg};{border}">'
                f'{label}</th>'
            )

        # ── Task sidebar rows ──────────────────────────────────────────────
        if tasks:
            task_rows = ""
            for t in tasks:
                tid_      = t["id"]
                cc        = cat_color.get(t["category"], "#64748b")
                safe_name = t["task_name"].replace("'", "\\'")
                cat_hint  = ("· " + t["category"]) if t["category"] else ""
                tkey      = t.get("test_key", "") or ""
                if tkey:
                    raw = sample_counts.get(tkey, None)
                    n_val = _extract_n(raw) if raw is not None else 0
                    n_samp = str(n_val) if n_val > 0 else "TBD"
                else:
                    # Prep/analysis tasks with no test key → show total n
                    n_samp = str(total_n) if total_n > 0 else "TBD"
                # Always show the n= pill; grey it out when TBD
                if n_samp == "TBD":
                    samp_pill = (
                        f'<span style="background:#f3f4f6;color:#9ca3af;border:1px solid #e5e7eb;'
                        f'border-radius:10px;font-size:.68rem;padding:1px 6px;'
                        f'font-weight:600;white-space:nowrap">n=TBD</span>'
                    )
                else:
                    samp_pill = (
                        f'<span style="background:#f0fdf4;color:#15803d;border:1px solid #bbf7d0;'
                        f'border-radius:10px;font-size:.68rem;padding:1px 6px;'
                        f'font-weight:600;white-space:nowrap">n={n_samp}</span>'
                    )
                # Second line: category dot + n= pill (always visible below the name)
                meta_line = ""
                if cat_hint or samp_pill:
                    sep = " &nbsp;·&nbsp; " if cat_hint and samp_pill else ""
                    meta_line = (f'<span style="font-size:.7rem;color:#9ca3af;display:block;'
                                 f'padding-left:14px;margin-top:1px">'
                                 f'{cat_hint}{sep}{samp_pill}</span>')
                task_rows += (
                    f'<tr id="tr-{tid_}" onclick="ganttSelectRow({tid_})" style="cursor:default">'
                    f'<td style="padding:6px 8px;max-width:220px" title="{t["task_name"]}">'
                    f'<span class="gantt-drag-handle" title="Drag to reorder" '
                    f'style="cursor:grab;color:#d1d5db;margin-right:4px;font-size:.85rem;'
                    f'vertical-align:middle;user-select:none">⠿</span>'
                    f'<span style="display:inline-block;width:9px;height:9px;'
                    f'border-radius:50%;background:{cc};margin-right:5px;flex-shrink:0;vertical-align:middle"></span>'
                    f'<span style="font-size:.82rem;white-space:nowrap;overflow:hidden;'
                    f'text-overflow:ellipsis;max-width:175px;display:inline-block;vertical-align:middle">'
                    f'{t["task_name"]}</span>'
                    f'{meta_line}'
                    f'</td>'
                    f'<td style="padding:6px 4px;text-align:center;white-space:nowrap">{status_badge(t["status"])}</td>'
                    f'<td style="padding:6px 4px;text-align:center;white-space:nowrap">'
                    f'<button class="btn btn-sm" style="padding:2px 7px;font-size:.73rem;'
                    f'border:1px solid #e0e0e0;border-radius:4px;background:#fff" '
                    f"onclick=\"event.stopPropagation();openEditModal({tid_},'{safe_name}','{t['category']}',"
                    f"{t['start_week']},{t['duration']},'{t['status']}')\""
                    f'>Edit</button> '
                    f'<form method="post" action="/projects/{pid}/tracker/task/{tid_}/delete"'
                    f' class="d-inline" onsubmit="return confirm(\'Delete task?\')">'
                    f'<button class="btn btn-sm" style="padding:2px 7px;font-size:.73rem;'
                    f'border:1px solid #fca5a5;border-radius:4px;background:#fff;color:#ef4444"'
                    f' onclick="event.stopPropagation()"'
                    f'>Del</button></form>'
                    f'</td></tr>'
                )
        else:
            task_rows = ('<tr><td colspan="3" class="text-center text-muted py-4"'
                         ' style="font-size:.85rem">No tasks yet.</td></tr>')

        # ── GANTT bar rows ─────────────────────────────────────────────────
        # Also build JS task-position data for edit mode
        task_pos_entries = []
        gantt_rows = ""
        for t in tasks:
            bar_bg, _ = _GANTT_STATUS_COLORS.get(t["status"], ("#9ca3af","#6b7280"))
            task_pos_entries.append(
                f'{t["id"]}:{{sw:{t["start_week"]},dur:{t["duration"]},color:"{bar_bg}"}}'
            )
            cells = ""
            for w in range(1, n_weeks + 1):
                in_range = t["start_week"] <= w <= t["start_week"] + t["duration"] - 1
                is_start = w == t["start_week"]
                is_end   = w == t["start_week"] + t["duration"] - 1
                is_now   = (current_week == w)
                now_attr = ' data-now="1"' if is_now else ""
                if in_range:
                    bg, fg = _GANTT_STATUS_COLORS.get(t["status"], ("#9ca3af","#6b7280"))
                    br = ("border-radius:4px;" if (is_start and is_end)
                          else "border-radius:4px 0 0 4px;" if is_start
                          else "border-radius:0 4px 4px 0;" if is_end else "")
                    now_border = "border-left:2px solid #f59e0b;" if is_now else ""
                    cells += (f'<td data-tid="{t["id"]}" data-week="{w}"{now_attr}'
                               f' style="background:{bg};{br}{now_border}'
                               f'padding:0;height:26px"></td>')
                else:
                    bg_cell   = "#fef9c3" if is_now else "#f9fafb"
                    bl        = "border-left:2px solid #f59e0b;" if is_now else (
                                "border-left:2px solid #d1d5db;" if w % 4 == 1 else
                                "border-left:1px solid #e5e7eb;")
                    cells += (f'<td data-tid="{t["id"]}" data-week="{w}"{now_attr}'
                               f' style="background:{bg_cell};{bl}padding:0;height:26px"></td>')
            gantt_rows += f'<tr data-gantt-row="{t["id"]}">{cells}</tr>'

        if not tasks:
            gantt_rows = (f'<tr><td colspan="{n_weeks}" class="text-center text-muted py-4"'
                          f' style="font-size:.85rem">Add tasks to see the chart.</td></tr>')

        task_pos_js = "{" + ",".join(task_pos_entries) + "}"

        # ── Options HTML ───────────────────────────────────────────────────
        status_opts_html = "".join(
            f'<option value="{k}">{v}</option>'
            for k, v in _GANTT_STATUS_LABELS.items()
        )

        # ── Legend ─────────────────────────────────────────────────────────
        legend = "".join(
            f'<span style="display:inline-flex;align-items:center;margin-right:12px;font-size:.75rem">'
            f'<span style="width:12px;height:12px;border-radius:3px;background:{bg};'
            f'display:inline-block;margin-right:4px"></span>{_GANTT_STATUS_LABELS[s]}</span>'
            for s, (bg, _fg) in _GANTT_STATUS_COLORS.items()
        )
        cat_legend = "".join(
            f'<span style="display:inline-flex;align-items:center;margin-right:12px;font-size:.75rem">'
            f'<span style="width:10px;height:10px;border-radius:50%;background:{cc};'
            f'display:inline-block;margin-right:4px"></span>{c}</span>'
            for c, cc in cat_color.items()
        )

        # ── Buttons ────────────────────────────────────────────────────────
        undo_btn = (
            f'<form method="post" action="/projects/{pid}/tracker" class="d-inline" id="undoForm">'
            f'<input type="hidden" name="action" value="undo">'
            f'<button type="submit" class="btn btn-sm ms-1" id="undoBtn" '
            f'style="border:1px solid #93c5fd;color:#1d4ed8;background:#eff6ff;font-size:.8rem">'
            f'<i class="bi bi-arrow-counterclockwise me-1"></i>Undo <kbd style="font-size:.68rem;'
            f'background:#dbeafe;border:1px solid #93c5fd;border-radius:3px;padding:0 4px">⌘Z</kbd>'
            f'</button></form>'
        ) if has_history else ""

        seed_btn = "" if tasks else (
            f'<form method="post" action="/projects/{pid}/tracker" class="d-inline ms-2">'
            f'<input type="hidden" name="action" value="seed">'
            f'<button class="btn btn-sm" style="background:var(--df-accent);color:#fff;'
            f'border:none;font-size:.8rem"><i class="bi bi-magic me-1"></i>Seed from JEDEC Template'
            f'</button></form>'
        )
        clear_btn = "" if not tasks else (
            f'<form method="post" action="/projects/{pid}/tracker" class="d-inline ms-2"'
            f' onsubmit="return confirm(\'Clear all tasks from this schedule? This cannot be undone.\')">'
            f'<input type="hidden" name="action" value="clear">'
            f'<button class="btn btn-sm" style="border:1px solid #fca5a5;color:#ef4444;'
            f'background:#fff;font-size:.8rem"><i class="bi bi-trash me-1"></i>Clear Schedule'
            f'</button></form>'
        )

        # ── Start-date picker ──────────────────────────────────────────────
        date_picker = (
            f'<form method="post" action="/projects/{pid}/tracker" '
            f'class="d-inline-flex align-items-center ms-3 gap-2">'
            f'<input type="hidden" name="action" value="set_start">'
            f'<label style="font-size:.75rem;color:var(--df-grey);white-space:nowrap">Start date</label>'
            f'<input type="date" name="start_date" value="{anchor.isoformat()}" '
            f'class="form-control form-control-sm" style="width:145px;font-size:.8rem">'
            f'<button type="submit" class="btn btn-sm" '
            f'style="border:1px solid var(--df-border);font-size:.78rem;white-space:nowrap">Set</button>'
            f'</form>'
        )

        return f"""
        <!-- Edit-task modal -->
        <div class="modal fade" id="editModal" tabindex="-1">
          <div class="modal-dialog">
            <div class="modal-content">
              <form method="post" id="editForm">
                <div class="modal-header">
                  <h6 class="modal-title mb-0">Edit Task</h6>
                  <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                  <div class="mb-3">
                    <label class="form-label" style="font-size:.83rem">Task Name</label>
                    <input type="text" class="form-control form-control-sm" name="task_name" id="e_name" required>
                  </div>
                  <div class="mb-3">
                    <label class="form-label" style="font-size:.83rem">Category</label>
                    <input type="text" class="form-control form-control-sm" name="category" id="e_cat"
                           placeholder="Preparation / Stress / Analysis / Reporting">
                  </div>
                  <div class="row g-2">
                    <div class="col">
                      <label class="form-label" style="font-size:.83rem">
                        Start Week
                        <span class="text-muted" style="font-size:.72rem;font-weight:400"> — set via Edit mode</span>
                      </label>
                      <input type="number" class="form-control form-control-sm" name="start_week" id="e_sw"
                             min="1" max="104" readonly
                             style="background:#f3f4f6;color:#6b7280;cursor:not-allowed">
                    </div>
                    <div class="col">
                      <label class="form-label" style="font-size:.83rem">
                        Duration (weeks)
                        <span class="text-muted" style="font-size:.72rem;font-weight:400"> — set via Edit mode</span>
                      </label>
                      <input type="number" class="form-control form-control-sm" name="duration" id="e_dur"
                             min="1" max="52" readonly
                             style="background:#f3f4f6;color:#6b7280;cursor:not-allowed">
                    </div>
                  </div>
                  <div class="mt-3">
                    <label class="form-label" style="font-size:.83rem">Status</label>
                    <select class="form-select form-select-sm" name="status" id="e_status">
                      {status_opts_html}
                    </select>
                  </div>
                </div>
                <div class="modal-footer">
                  <button type="button" class="btn btn-sm btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
                  <button type="submit" class="btn btn-sm"
                    style="background:var(--df-accent);color:#fff;border:none">Save</button>
                </div>
              </form>
            </div>
          </div>
        </div>

        <div class="d-flex align-items-center justify-content-between mb-3 flex-wrap gap-2">
          <h5 class="mb-0" style="font-weight:300">Schedule — {p['name']}</h5>
          <div class="d-flex align-items-center flex-wrap gap-1">
            {date_picker}
            {undo_btn}
            {seed_btn}
            {clear_btn}
            <!-- Edit mode toggle -->
            <button id="ganttEditBtn" class="btn btn-sm ms-1" onclick="ganttToggleEdit()"
              style="border:1px solid #c4b5fd;color:#6d28d9;background:#fff;font-size:.8rem">
              <i class="bi bi-pencil-square me-1"></i>Edit
            </button>
            <!-- Save button (hidden until edit mode active) -->
            <button id="ganttSaveBtn" class="btn btn-sm ms-1" onclick="ganttSave()"
              style="display:none;background:#16a34a;color:#fff;border:none;font-size:.8rem">
              <i class="bi bi-check2 me-1"></i>Save
            </button>
            <!-- Delete selection (hidden until cells selected) -->
            <button id="ganttDelSelBtn" class="btn btn-sm ms-1" onclick="ganttDeleteSel()"
              style="display:none;border:1px solid #fca5a5;color:#dc2626;background:#fff;font-size:.8rem">
              <i class="bi bi-eraser me-1"></i>Delete
            </button>
            <button class="btn btn-sm ms-1" style="border:1px solid var(--df-border);font-size:.8rem"
              data-bs-toggle="collapse" data-bs-target="#addTaskPanel">
              <i class="bi bi-plus-lg me-1"></i>Add Task
            </button>
          </div>
        </div>
        <!-- Edit mode hint bar -->
        <div id="editHint" style="display:none;font-size:.78rem;color:#6d28d9;
          background:#f5f3ff;border:1px solid #c4b5fd;border-radius:6px;
          padding:6px 12px;margin-bottom:8px">
          <i class="bi bi-pencil-square me-1"></i>
          <strong>Edit mode:</strong> Click a row in the task list to select it,
          then click &amp; drag on the calendar to draw the bar.
          Click filled cells to select them for deletion.
          Click <strong>Save</strong> to commit, or <strong>Edit</strong> again to cancel.
        </div>

        <!-- Add task panel -->
        <div class="collapse mb-3" id="addTaskPanel">
          <div class="card card-body p-3" style="border:1px solid var(--df-border)">
            <form method="post" action="/projects/{pid}/tracker" class="row g-2 align-items-end">
              <input type="hidden" name="action" value="add">
              <div class="col-12 col-md-4">
                <label class="form-label mb-1" style="font-size:.78rem">Task Name</label>
                <input type="text" class="form-control form-control-sm" name="task_name"
                       placeholder="e.g. Temperature Cycling" required>
              </div>
              <div class="col-6 col-md-2">
                <label class="form-label mb-1" style="font-size:.78rem">Category</label>
                <input type="text" class="form-control form-control-sm" name="category"
                       placeholder="Stress">
              </div>
              <div class="col-6 col-md-1">
                <label class="form-label mb-1" style="font-size:.78rem">Start Wk</label>
                <input type="number" class="form-control form-control-sm" name="start_week" value="1" min="1">
              </div>
              <div class="col-6 col-md-1">
                <label class="form-label mb-1" style="font-size:.78rem">Duration</label>
                <input type="number" class="form-control form-control-sm" name="duration" value="1" min="1">
              </div>
              <div class="col-6 col-md-2">
                <label class="form-label mb-1" style="font-size:.78rem">Status</label>
                <select class="form-select form-select-sm" name="status">
                  {status_opts_html}
                </select>
              </div>
              <div class="col-12 col-md-2">
                <button class="btn btn-sm w-100"
                  style="background:var(--df-accent);color:#fff;border:none">Add Task</button>
              </div>
            </form>
          </div>
        </div>

        <!-- Bulk save form (hidden, submitted by JS) -->
        <form id="ganttBulkForm" method="post" action="/projects/{pid}/tracker" style="display:none">
          <input type="hidden" name="action" value="bulk_edit">
          <input type="hidden" name="changes" id="ganttBulkChanges">
        </form>

        <!-- Main layout -->
        <div class="card shadow-sm mb-3" style="overflow:hidden" id="ganttCard">
          <div style="display:flex;overflow-x:auto;min-width:0">

            <!-- Sidebar -->
            <div style="min-width:460px;max-width:460px;border-right:2px solid #e5e7eb;flex-shrink:0">
              <table class="table table-sm mb-0" style="border-radius:0;table-layout:fixed">
                <thead>
                  <tr style="background:#f3f4f6">
                    <th style="padding:8px;font-size:.73rem;color:#6b7280;font-weight:600;width:65%">TASK</th>
                    <th style="padding:8px;font-size:.73rem;color:#6b7280;font-weight:600;text-align:center">STATUS</th>
                    <th style="padding:8px;font-size:.73rem;color:#6b7280;font-weight:600;width:80px"></th>
                  </tr>
                </thead>
                <tbody id="ganttSidebody">{task_rows}</tbody>
              </table>
            </div>

            <!-- Chart -->
            <div style="overflow-x:auto;flex:1 1 auto">
              <table id="ganttChartTable" style="border-collapse:collapse;height:100%;user-select:none">
                <thead>
                  <tr style="background:#f3f4f6">{month_headers}</tr>
                  <tr style="background:#f9fafb">{wk_headers}</tr>
                </thead>
                <tbody id="ganttChartBody">{gantt_rows}</tbody>
              </table>
            </div>

          </div>
        </div>

        <!-- Legends -->
        <div class="d-flex flex-wrap gap-3 align-items-center" style="font-size:.75rem;color:#6b7280">
          <div><strong>Status:</strong> {legend}</div>
          {"" if not cat_legend else f"<div><strong>Category:</strong> {cat_legend}</div>"}
        </div>

        <script src="https://cdnjs.cloudflare.com/ajax/libs/Sortable/1.15.2/Sortable.min.js"></script>
        <script>
        // ── Edit-task modal ──────────────────────────────────────────────────────
        function openEditModal(tid, name, cat, sw, dur, status) {{
          document.getElementById('e_name').value   = name;
          document.getElementById('e_cat').value    = cat;
          document.getElementById('e_sw').value     = sw;
          document.getElementById('e_dur').value    = dur;
          document.getElementById('e_status').value = status;
          document.getElementById('editForm').action =
            '/projects/{pid}/tracker/task/' + tid + '/edit';
          bootstrap.Modal.getOrCreateInstance(document.getElementById('editModal')).show();
        }}

        // ── Keyboard shortcuts ───────────────────────────────────────────────────
        document.addEventListener('keydown', function(e) {{
          // Cmd+Z / Ctrl+Z → undo
          if ((e.metaKey || e.ctrlKey) && e.key === 'z' && !e.shiftKey) {{
            const uf = document.getElementById('undoForm');
            if (uf) {{ e.preventDefault(); uf.submit(); }}
          }}
          // Delete / Backspace → commit red-cell deletion when in edit mode
          if ((e.key === 'Delete' || e.key === 'Backspace') && editActive && delSel.size > 0) {{
            // Only fire if focus is not inside a text input / textarea
            const tag = document.activeElement ? document.activeElement.tagName : '';
            if (tag !== 'INPUT' && tag !== 'TEXTAREA' && tag !== 'SELECT') {{
              e.preventDefault();
              ganttDeleteSel();
            }}
          }}
        }});

        // ── GANTT Edit Mode ──────────────────────────────────────────────────────
        // Task positions from server: {{taskId: {{sw, dur, color}}}}
        const GANTT_TASK_POS = {task_pos_js};

        // Track filled weeks per task (mutable during edit session)
        const filledWeeks = {{}};   // taskId → Set<week>
        const dirtyTids   = new Set();
        let   selTid      = null;
        let   drag        = null;   // {{mode:'fill'|'delete', start:N, cur:N}}
        const delSel      = new Set();  // weeks selected for deletion on selTid
        let   editActive  = false;

        // Initialise from server data
        for (const [tid, p] of Object.entries(GANTT_TASK_POS)) {{
          const s = new Set();
          for (let w = p.sw; w < p.sw + p.dur; w++) s.add(w);
          filledWeeks[tid] = s;
        }}

        // ── Toggle edit mode ───────────────────────────────────────────────
        function ganttToggleEdit() {{
          editActive = !editActive;
          const btn = document.getElementById('ganttEditBtn');
          const saveBtn = document.getElementById('ganttSaveBtn');
          const hint = document.getElementById('editHint');
          btn.style.background = editActive ? '#f5f3ff' : '#fff';
          btn.style.color      = editActive ? '#6d28d9' : '#6d28d9';
          btn.style.borderColor = editActive ? '#7c3aed' : '#c4b5fd';
          saveBtn.style.display = editActive ? '' : 'none';
          hint.style.display    = editActive ? '' : 'none';
          if (!editActive) {{
            ganttSelectRow(null);
            delSel.clear();
            ganttUpdateDelBtn();
          }}
          // Update sidebar row cursor
          document.querySelectorAll('#ganttSidebody tr').forEach(r => {{
            r.style.cursor = editActive ? 'pointer' : '';
          }});
        }}

        // ── Select a task row ──────────────────────────────────────────────
        function ganttSelectRow(tid) {{
          if (!editActive && tid !== null) return;
          // Deselect previous
          if (selTid !== null) {{
            const prev = document.getElementById('tr-' + selTid);
            if (prev) prev.style.background = '';
            ganttRenderRow(selTid);
          }}
          delSel.clear();
          ganttUpdateDelBtn();
          selTid = tid === null ? null : String(tid);
          if (selTid !== null) {{
            const row = document.getElementById('tr-' + selTid);
            if (row) row.style.background = '#fef3c7';
          }}
          ganttRenderAll();
        }}

        // ── Render a single chart row ──────────────────────────────────────
        function ganttRenderRow(tid) {{
          const row = document.querySelector('[data-gantt-row="' + tid + '"]');
          if (!row) return;
          const fw = filledWeeks[tid] || new Set();
          const color = (GANTT_TASK_POS[tid] || {{}}).color || '#9ca3af';
          const isSel = (String(tid) === selTid);
          row.querySelectorAll('td').forEach(cell => {{
            const w = parseInt(cell.dataset.week);
            if (!w) return;
            const isFill    = fw.has(w);
            const isNow     = cell.dataset.now === '1';
            const dragMin   = drag ? Math.min(drag.start, drag.cur) : 0;
            const dragMax   = drag ? Math.max(drag.start, drag.cur) : 0;
            const inDrag    = drag && String(drag.tid) === String(tid) && w >= dragMin && w <= dragMax;
            const isDragFill = inDrag && drag.mode === 'fill';
            const isDragDel  = inDrag && drag.mode === 'delete';
            const isDelSel   = isSel && delSel.has(w);
            let bg, cur;
            if (isDragFill) {{
              bg = '#93c5fd'; cur = 'cell';
            }} else if (isDragDel || isDelSel) {{
              bg = '#fca5a5'; cur = 'pointer';
            }} else if (isFill) {{
              bg = color;
              cur = (editActive && isSel) ? 'pointer' : 'default';
            }} else {{
              bg = isNow ? '#fef9c3' : (isSel && editActive ? '#ede9fe' : '#f9fafb');
              cur = (editActive && isSel) ? 'crosshair' : 'default';
            }}
            cell.style.background = bg;
            cell.style.cursor = cur;
          }});
        }}

        function ganttRenderAll() {{
          for (const tid of Object.keys(GANTT_TASK_POS)) ganttRenderRow(tid);
        }}

        // ── Mouse events on chart ──────────────────────────────────────────
        const chartTable = document.getElementById('ganttChartTable');
        if (chartTable) {{
          chartTable.addEventListener('mousedown', function(e) {{
            if (!editActive || !selTid) return;
            const cell = e.target.closest('[data-tid]');
            if (!cell || String(cell.dataset.tid) !== selTid) return;
            e.preventDefault();
            const w = parseInt(cell.dataset.week);
            const isFill = (filledWeeks[selTid] || new Set()).has(w);
            drag = {{mode: isFill ? 'delete' : 'fill', start: w, cur: w, tid: selTid}};
            ganttRenderRow(selTid);
          }});

          chartTable.addEventListener('mousemove', function(e) {{
            if (!drag) return;
            const cell = e.target.closest('[data-tid]');
            if (!cell || String(cell.dataset.tid) !== drag.tid) return;
            const w = parseInt(cell.dataset.week);
            if (w !== drag.cur) {{
              drag.cur = w;
              ganttRenderRow(selTid);
            }}
          }});
        }}

        document.addEventListener('mouseup', function() {{
          if (!drag) return;
          const tid = drag.tid;
          const min = Math.min(drag.start, drag.cur);
          const max = Math.max(drag.start, drag.cur);
          const fw  = filledWeeks[tid] || new Set();
          if (drag.mode === 'fill') {{
            for (let w = min; w <= max; w++) fw.add(w);
            delSel.clear();
            dirtyTids.add(parseInt(tid));
          }} else {{
            // Add to delete selection (only filled cells)
            for (let w = min; w <= max; w++) {{
              if (fw.has(w)) delSel.add(w);
            }}
            ganttUpdateDelBtn();
          }}
          drag = null;
          ganttRenderRow(tid);
        }});

        // ── Delete selection ───────────────────────────────────────────────
        function ganttDeleteSel() {{
          if (!selTid) return;
          const fw = filledWeeks[selTid] || new Set();
          delSel.forEach(w => fw.delete(w));
          delSel.clear();
          ganttUpdateDelBtn();
          dirtyTids.add(parseInt(selTid));
          ganttRenderRow(selTid);
        }}

        function ganttUpdateDelBtn() {{
          document.getElementById('ganttDelSelBtn').style.display =
            (delSel.size > 0) ? '' : 'none';
        }}

        // ── Save bulk edits ────────────────────────────────────────────────
        function ganttSave() {{
          // Flush any pending red-cell deletion before computing final state
          if (delSel.size > 0) ganttDeleteSel();

          const changes = [];
          dirtyTids.forEach(tid => {{
            const fw = filledWeeks[tid] || new Set();
            if (fw.size === 0) return;  // no bar → skip
            const weeks = [...fw].sort((a, b) => a - b);
            changes.push({{
              id:         tid,
              start_week: weeks[0],
              duration:   weeks[weeks.length - 1] - weeks[0] + 1
            }});
          }});
          if (changes.length === 0) {{
            ganttToggleEdit();  // nothing changed, just exit
            return;
          }}
          document.getElementById('ganttBulkChanges').value = JSON.stringify(changes);
          document.getElementById('ganttBulkForm').submit();
        }}

        // ── Drag-to-reorder sidebar rows ───────────────────────────────────
        (function() {{
          const sideBody = document.getElementById('ganttSidebody');
          const chartBody = document.getElementById('ganttChartBody');
          if (!sideBody || typeof Sortable === 'undefined') return;

          Sortable.create(sideBody, {{
            handle: '.gantt-drag-handle',
            animation: 150,
            ghostClass: 'sortable-ghost',
            onEnd: function() {{
              // Collect new order from sidebar
              const rows = sideBody.querySelectorAll('tr[id^="tr-"]');
              const orderedIds = [...rows].map(r => r.id.replace('tr-', ''));

              // Sync chart body rows to same order
              orderedIds.forEach(tid => {{
                const cr = chartBody.querySelector('[data-gantt-row="' + tid + '"]');
                if (cr) chartBody.appendChild(cr);
              }});

              // Persist new order to server (fire-and-forget)
              fetch('/projects/{pid}/tracker', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                body: 'action=reorder&order=' + encodeURIComponent(JSON.stringify(orderedIds))
              }}).catch(() => {{}});
            }}
          }});
        }})();
        </script>
        """


class ProjectTaskHandler(Base):
    """Handles edit and delete of individual GANTT tasks."""

    def post(self, pid, tid, action):
        p = _db.get_project(int(pid))
        if not p:
            self.send_error(404); return
        tid = int(tid)

        # Fetch current task state before mutating (for undo)
        all_tasks = _db.list_gantt_tasks(p["id"])
        prev = next((t for t in all_tasks if t["id"] == tid), None)

        if action == "delete":
            if prev:
                _db.push_gantt_history(p["id"], "delete_task", prev)
            _db.delete_gantt_task(tid, p["id"])
        elif action == "edit":
            if prev:
                _db.push_gantt_history(p["id"], "edit_task", prev)
            _db.update_gantt_task(
                tid, p["id"],
                task_name  = self.get_argument("task_name",  ""),
                category   = self.get_argument("category",   ""),
                start_week = int(self.get_argument("start_week",  "1")),
                duration   = int(self.get_argument("duration",    "1")),
                status     = self.get_argument("status", "not_started"),
            )
        self.redirect(f"/projects/{p['id']}/tracker")


# ── App routing & entry point ─────────────────────────────────────────────────

def make_app():
    specs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "specs")
    return tornado.web.Application(
        [
            (r"/",                                   IndexHandler),
            (r"/part-type",                          PartTypeHandler),
            (r"/lookup",                             LookupHandler),
            (r"/sample-size",                        SampleSizeHandler),
            (r"/pass-fail",                          PassFailHandler),
            (r"/report",                             ReportHandler),
            (r"/report/pdf",                         ReportPdfHandler),
            # ── Project management ──────────────────────────────────────────
            (r"/projects",                           ProjectListHandler),
            (r"/projects/new",                       ProjectNewHandler),
            (r"/projects/(\d+)",                     ProjectDetailHandler),
            (r"/projects/(\d+)/delete",              ProjectDeleteHandler),
            (r"/projects/(\d+)/meta",                ProjectMetaHandler),
            (r"/projects/(\d+)/sample-size",         ProjectSampleSizeHandler),
            (r"/projects/(\d+)/pass-fail",           ProjectPassFailHandler),
            (r"/projects/(\d+)/report",              ProjectReportHandler),
            (r"/projects/(\d+)/report/pdf",          ProjectReportPdfHandler),
            (r"/projects/(\d+)/csam",                ProjectCsamHandler),
            (r"/projects/(\d+)/csam/(\d+)",          ProjectCsamImageHandler),
            (r"/projects/(\d+)/csam/(\d+)/thumb",    ProjectCsamThumbHandler),
            (r"/projects/(\d+)/csam/(\d+)/delete",   ProjectCsamDeleteHandler),
            (r"/projects/(\d+)/tracker",             ProjectTrackerHandler),
            (r"/projects/(\d+)/tracker/task/(\d+)/(edit|delete)", ProjectTaskHandler),
            # ── Static spec PDFs ────────────────────────────────────────────
            (r"/specs/(.*)",                         tornado.web.StaticFileHandler,
                                                     {"path": specs_path}),
        ],
        cookie_secret=COOKIE_SECRET,
        debug=False,
    )


if __name__ == "__main__":
    _db.init_db()          # create / migrate SQLite schema on first run
    PORT = int(os.environ.get("PORT", 5001))
    app = make_app()
    app.listen(PORT, address="0.0.0.0")
    url = f"http://localhost:{PORT}"
    print(f"\n  Package Reliability Qualification Suite")
    print(f"  ──────────────────────────────────────────")
    print(f"  Server running at {url}")
    print(f"  Press Ctrl+C to stop.\n")
    if PORT == 5001:
        webbrowser.open(url)
    tornado.ioloop.IOLoop.current().start()
