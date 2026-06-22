#!/usr/bin/env python3
"""
JEDEC Test Protocol Calculator
Reliability qualification toolkit — Diamond Foundry Inc.
"""

import math
import sys
import os
from datetime import datetime

# ─── Chi-squared statistics (pure stdlib, no dependencies) ───────────────────
#
# WHY CHI-SQUARED?
# ─────────────────
# JEDEC reliability qualification uses the chi-squared distribution to make
# statistically rigorous statements like:
#   "We tested N units and observed K failures. We are C% confident that
#    the true failure rate in the population is no worse than (1 - R)."
#
# The connection to chi-squared comes from the Poisson process model of
# failures. If failures occur randomly at a constant rate, the number of
# failures k in a test of n units follows a Poisson distribution. The exact
# confidence interval on the Poisson rate parameter maps directly to the
# chi-squared distribution — this is a standard result in reliability
# statistics (see MIL-HDBK-189C or JESD47I for the derivation).
#
# Specifically:  chi²(2k+2, C) is the C-th percentile of a chi-squared
# distribution with 2(k+1) degrees of freedom. This value acts as an
# upper bound on 2·n·λ (twice the total device-hours × failure rate),
# which lets us back-calculate either the required sample size or the
# demonstrated reliability after the test.
#
# BUILDING BLOCKS (bottom-up):
#   1. _reg_gamma_inc   — regularized incomplete gamma function P(a,x)
#   2. _chi2_cdf        — chi-squared CDF built on top of P(a,x)
#   3. _chi2_ppf        — inverse CDF (quantile function), needed to find
#                         the chi-squared value for a given confidence level
#   4. min_sample_size / demonstrated_reliability — the JEDEC formulas

def _reg_gamma_inc(a: float, x: float) -> float:
    """
    Regularized lower incomplete gamma function P(a, x).

    Definition:
        P(a, x) = γ(a, x) / Γ(a)
    where γ(a, x) = ∫₀ˣ t^(a-1) · e^(−t) dt  (lower incomplete gamma)
    and   Γ(a)    = ∫₀^∞ t^(a-1) · e^(−t) dt  (complete gamma function)

    P(a, x) ranges from 0 to 1 and is essentially a cumulative probability.
    It is the foundation of the chi-squared CDF: chi2_cdf(x, df) = P(df/2, x/2).

    Two numerically stable algorithms are used depending on where x sits
    relative to a+1 (the crossover point where each method converges fastest):

    x < a+1  →  Series expansion (converges quickly near x=0)
    x ≥ a+1  →  Lentz continued fraction (converges quickly for large x)

    Both algorithms work in log-space (using math.lgamma) to avoid overflow
    when a is large.
    """
    if x < 0:
        return 0.0
    if x == 0:
        return 0.0

    # math.lgamma(a) = ln(Γ(a)); used to normalize without computing Γ(a) directly,
    # which would overflow for large a.
    log_gamma_a = math.lgamma(a)

    if x < a + 1.0:
        # ── Series expansion ──────────────────────────────────────────────────
        # γ(a, x) = e^(−x) · x^a · Σ_{n=0}^∞  x^n / (a · (a+1) · … · (a+n))
        #
        # We accumulate the sum term-by-term. Each term = previous term × x/(a+n).
        # 'delta' is the running sum; 'term' is the current term.
        # We start with term = 1/a (the n=0 term after factoring out 1/a).
        ap = a
        delta = term = 1.0 / a
        for _ in range(200):
            ap += 1.0
            term *= x / ap        # multiply by x/(a+n) to get next term
            delta += term
            # Converged when the new term is negligible relative to the sum
            if abs(term) < abs(delta) * 1e-12:
                break
        # Multiply by the prefactor e^(−x) · x^a / Γ(a), done in log-space
        return delta * math.exp(-x + a * math.log(x) - log_gamma_a)

    else:
        # ── Lentz continued fraction for the upper incomplete gamma ───────────
        # The UPPER incomplete gamma Γ(a,x) = Γ(a) − γ(a,x) has a continued
        # fraction representation that converges well for x ≥ a+1:
        #
        #   Γ(a,x) = e^(−x) · x^a · CF
        #
        # where CF is evaluated using Lentz's algorithm, which builds the
        # fraction iteratively without storing all terms at once.
        #
        # FPMIN guards against division by zero in the Lentz recurrence.
        FPMIN = 1e-300
        b = x + 1.0 - a    # first term of the CF denominator
        c = 1.0 / FPMIN
        d = 1.0 / b
        h = d               # h accumulates the CF value
        for i in range(1, 201):
            # Numerator and denominator updates per Lentz recurrence
            an = -i * (i - a)
            b += 2.0
            d = an * d + b
            if abs(d) < FPMIN: d = FPMIN
            c = b + an / c
            if abs(c) < FPMIN: c = FPMIN
            d = 1.0 / d
            delta = d * c
            h *= delta
            # Converged when the multiplicative update is essentially 1.0
            if abs(delta - 1.0) < 1e-12:
                break
        # h is now the continued fraction value for the UPPER incomplete gamma.
        # Subtract from 1 to get the LOWER (regularized) value P(a,x).
        return 1.0 - math.exp(-x + a * math.log(x) - log_gamma_a) * h


def _chi2_cdf(x: float, df: float) -> float:
    """
    CDF of the chi-squared distribution with 'df' degrees of freedom.

    The chi-squared distribution with ν degrees of freedom is a special case
    of the gamma distribution: χ²(ν) = Gamma(shape=ν/2, rate=1/2).

    Therefore its CDF is exactly the regularized lower incomplete gamma:
        P(χ² ≤ x | ν) = P(ν/2, x/2)

    In reliability context, df = 2(k+1) where k = number of failures observed.
    The "+2" in the degrees of freedom accounts for the one-sided upper
    confidence bound on the Poisson failure rate.
    """
    if x <= 0:
        return 0.0
    return _reg_gamma_inc(df / 2.0, x / 2.0)


def _chi2_ppf(p: float, df: float) -> float:
    """
    Inverse CDF (percent-point function / quantile function) of chi-squared.

    Given a probability p and degrees of freedom df, returns x such that:
        P(χ² ≤ x | df) = p

    This is the critical value used in the JEDEC sample-size formula.
    For example, _chi2_ppf(0.90, 2) ≈ 4.605, meaning there is a 90% chance
    that a chi-squared variable with 2 d.f. is ≤ 4.605.

    Algorithm: two-stage approach
    ─────────────────────────────
    Stage 1 — Wilson-Hilferty warm start:
        The Wilson-Hilferty approximation transforms χ²(ν) to a near-normal
        variable:  χ²(ν) ≈ ν · (z·√(2/(9ν)) + 1 − 2/(9ν))³
        where z is the standard normal quantile for p.
        The normal quantile z is computed via the Beasley-Springer-Moro (BSM)
        rational approximation, which is accurate to ~5 decimal places.
        This gives a starting estimate x0 that is usually within a few percent
        of the true answer.

    Stage 2 — Bisection refinement:
        We bracket [lo, hi] around x0 and bisect until |hi - lo| < 1e-9,
        using _chi2_cdf as the oracle. 100 iterations gives ~30 bits of
        precision, more than enough for sample-size calculations.
    """
    if p <= 0:
        return 0.0
    if p >= 1:
        return math.inf

    # ── Stage 1: Wilson-Hilferty approximation for the normal quantile ────────
    # h = 1 − 2/(9ν) is the mean of the cube-root-transformed chi-squared
    h = 1.0 - 2.0 / (9.0 * df)

    # BSM rational approximation for the standard normal quantile z = Φ⁻¹(p).
    # Works by first computing z for the tail probability q = min(p, 1-p),
    # then flipping sign if p > 0.5.
    q = p if p <= 0.5 else 1.0 - p
    t = math.sqrt(-2.0 * math.log(q))   # initial transform
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    z = t - (c0 + c1*t + c2*t*t) / (1.0 + d1*t + d2*t*t + d3*t*t*t)
    if p > 0.5:
        z = -z

    # Apply Wilson-Hilferty: map z back to the chi-squared scale
    x0 = df * max(1e-6, (z * math.sqrt(1.0 / (9.0 * df)) + h) ** 3)

    # ── Stage 2: Bisection ────────────────────────────────────────────────────
    # Set a bracket [lo, hi] that is guaranteed to contain the true root.
    # Start hi at 4× the estimate; double it until the CDF exceeds p.
    lo, hi = 0.0, max(x0 * 4, df * 4, 100.0)
    while _chi2_cdf(hi, df) < p:
        hi *= 2.0

    # Binary search: after each step, [lo, hi] still brackets the root.
    for _ in range(100):
        mid = (lo + hi) / 2.0
        if _chi2_cdf(mid, df) < p:
            lo = mid   # root is above mid
        else:
            hi = mid   # root is below mid
        if (hi - lo) < 1e-9:
            break

    return (lo + hi) / 2.0


# ─── Preconditioning (universal precursor) ────────────────────────────────────

PRECOND = {
    "name":      "PC",
    "full_name": "Preconditioning (MSL)",
    "standard":  "JESD22-A113I / J-STD-020F",
    "condition": "Bake @125°C, 85°C/85%RH, 3× reflow (260°C peak)",
    "duration":  "24 hr bake, 168 hr soak, 3× reflow (7 min/cycle)",
    "pass_criteria": "No delamination by CSAM",
    "pre_testing":   "CSAM",
    "post_testing":  "CSAM",
    "notes": "Universal precursor — must be completed and verified by CSAM before any qualification test begins.",
}

# ─── JEDEC Test Database ──────────────────────────────────────────────────────

TESTS = {
    "uhast": {
        "name": "uHAST",
        "full_name": "uHAST — Unbiased HAST",
        "standard": "JESD22-A118B.01",
        "condition": "130°C / 85% RH / 33.3 psia",
        "duration": "96 hours",
        "sample_size": 5,
        "pass_criteria": "CSAM ≥95% bond area; Functionality: sensor reading within 5% of original (no heating); Thermal: sensor reading within 5% of original (with heating)",
        "pre_testing": "CSAM + Func + Thermal",
        "post_testing": "CSAM + Func + Thermal",
        "destructive": True,
        "active_devices": False,
        "notes": "Si/SCD level feasibility under assessment due to fixturing",
    },
    "tc": {
        "name": "TC",
        "full_name": "Temperature Cycling",
        "standard": "JESD22-A104F.01",
        "condition": "-55°C to +150°C (Condition H)",
        "duration": "1000 cycles at 2 cycles/hr (~21 days)",
        "sample_size": 5,
        "pass_criteria": "CSAM ≥95% bond area; Functionality: sensor reading within 5% of original (no heating); Thermal: sensor reading within 5% of original (with heating)",
        "pre_testing": "CSAM + Func + Thermal",
        "post_testing": "CSAM + Func + Thermal",
        "destructive": True,
        "active_devices": False,
        "notes": "",
    },
    "tshock": {
        "name": "T-Shock",
        "full_name": "Thermal Shock",
        "standard": "JESD22-A106B.02",
        "condition": "-55°C to +125°C (Condition C, air-to-air)",
        "duration": "200 cycles minimum",
        "sample_size": 5,
        "pass_criteria": "CSAM ≥95% bond area; Functionality: sensor reading within 5% of original (no heating); Thermal: sensor reading within 5% of original (with heating)",
        "pre_testing": "CSAM + Func + Thermal",
        "post_testing": "CSAM + Func + Thermal",
        "destructive": True,
        "active_devices": False,
        "notes": "Transfer time between hot and cold extremes must not exceed 20 seconds.",
    },
    "mshock": {
        "name": "M-Shock",
        "full_name": "Mechanical Shock",
        "standard": "JESD22-B110B.01",
        "condition": "Service Condition B — 1500G peak, 0.5 ms half-sine pulse",
        "duration": "30 shocks free state (5 pulses × ±X, ±Y, ±Z); or 12 shocks mounted state (2 pulses × ±X, ±Y, ±Z)",
        "sample_size": 5,
        "pass_criteria": "CSAM ≥95% bond area; Functionality: sensor reading within 5% of original (no heating); Thermal: sensor reading within 5% of original (with heating)",
        "pre_testing": "CSAM + Func + Thermal",
        "post_testing": "CSAM + Func + Thermal",
        "destructive": True,
        "active_devices": False,
        "notes": "",
    },
    "vib": {
        "name": "Vibration",
        "full_name": "Variable Frequency Vibration",
        "standard": "JESD22-B103B.01",
        "condition": "Test Condition 1 — 20–2000 Hz, 0.060 mm / 1.5g amplitude",
        "duration": "48 min total — 4 sweeps × 3 axes (X, Y, Z) = 12 sweeps × 4 min/sweep",
        "sample_size": 5,
        "pass_criteria": "CSAM ≥95% bond area; Functionality: sensor reading within 5% of original (no heating); Thermal: sensor reading within 5% of original (with heating)",
        "pre_testing": "CSAM + Func + Thermal",
        "post_testing": "CSAM + Func + Thermal",
        "destructive": True,
        "active_devices": False,
        "notes": "Sweep rate: 1 decade/min, logarithmic. Each sweep traverses min→max→min frequency in 4 min. Tolerance ±10% on displacement or acceleration.",
    },
    "pc": {
        "name": "Pwr Cycling",
        "full_name": "Power Cycling",
        "standard": "JESD22-A122",
        "condition": "Up to +125°C with ΔT ≥ 100°C",
        "duration": "1000 cycles",
        "sample_size": 5,
        "pass_criteria": "CSAM ≥95% bond area; Functionality: sensor reading within 5% of original (no heating); Thermal: sensor reading within 5% of original (with heating)",
        "pre_testing": "CSAM + Func + Thermal",
        "post_testing": "CSAM + Func + Thermal",
        "destructive": True,
        "active_devices": False,
        "notes": "Package level only for Phase 1",
    },
    "ptc": {
        "name": "PTC",
        "full_name": "Power and Temperature Cycling",
        "standard": "JESD22-A105D",
        "condition": "Cond A: -40°C to +85°C (20 min transition, 10 min dwell); Cond B: -40°C to +125°C (30 min transition, 10 min dwell); power 5 min on / 5 min off",
        "duration": "Per applicable specification (typically 200–500 cycles)",
        "sample_size": 5,
        "pass_criteria": "No parametric limit exceedances; functionality maintained per applicable spec",
        "pre_testing": "Functional",
        "post_testing": "Functional",
        "destructive": True,
        "active_devices": True,
        "notes": "Biasing conditions per applicable specification",
    },
    "hts": {
        "name": "HTS",
        "full_name": "High Temperature Storage",
        "standard": "JESD22-A103D",
        "condition": "175°C in atmosphere",
        "duration": "1000 hours (~42 days)",
        "sample_size": 5,
        "pass_criteria": "CSAM ≥95% bond area; Functionality: sensor reading within 5% of original (no heating); Thermal: sensor reading within 5% of original (with heating)",
        "pre_testing": "CSAM + Func + Thermal",
        "post_testing": "CSAM + Func + Thermal",
        "destructive": True,
        "active_devices": False,
        "notes": "Low priority for Phase 1",
    },
    "shadow_moire": {
        "name": "Shadow Moiré",
        "full_name": "Shadow Moiré — Warpage Characterization",
        "standard": "JESD22-B112C",
        "condition": "RT→150°C→200°C→217°C→260°C (reflow profile)",
        "duration": "Follow reflow conditions",
        "sample_size": 3,
        "pass_criteria": "Characterization only — no pass/fail threshold. Reports signed peak-to-valley warpage (µm or mil) vs. temperature. Positive = convex/frowning, negative = concave/smiling.",
        "pre_testing": "3D contour baseline at RT",
        "post_testing": "N/A",
        "destructive": False,
        "active_devices": False,
        "characterization_only": True,
        "notes": "Per JESD22-B112C §8, this is a characterization measurement. Mandatory outputs: signed warpage vs. temperature plot (§8.3) and peak-to-valley over soldering footprint (§8.1–8.2). 3D contour plots (§8.4) and diagonal line scans (§8.5) are optional. TTV package only.",
    },
    "htol": {
        "name": "HTOL",
        "full_name": "HTOL — High-Temp Operating Life",
        "standard": "JESD22-A108G / JESD85",
        "condition": "125°C, bias applied at max operating voltage",
        "duration": "168 hrs / 500 hrs / 1000 hrs",
        "sample_size": None,
        "pass_criteria": "No parametric or functional failures",
        "pre_testing": "Functional",
        "post_testing": "Functional",
        "destructive": False,
        "active_devices": True,
        "notes": "",
    },
    "elfr": {
        "name": "ELFR",
        "full_name": "ELFR — Early Life Failure Rate",
        "standard": "JESD22-A108G / JESD74A",
        "condition": "125°C, 48 hrs; 3000 production-grade samples",
        "duration": "48 hours",
        "sample_size": 3000,
        "pass_criteria": "No early-life failures; demonstrated FIT rate within target",
        "pre_testing": "Functional",
        "post_testing": "Functional",
        "destructive": False,
        "active_devices": True,
        "notes": "",
    },
    "thb": {
        "name": "THB",
        "full_name": "THB — Temp Humidity Bias",
        "standard": "JESD22-A110",
        "condition": "130°C / 85% RH, Vdd_max (Condition A); or 110°C / 85% RH (Condition B)",
        "duration": "96 hrs (Cond A) / 264 hrs (Cond B)",
        "sample_size": None,
        "pass_criteria": "No parametric limit exceedances; functionality maintained under nominal and worst-case conditions per applicable procurement document or data sheet",
        "pre_testing": "Functional",
        "post_testing": "Functional",
        "destructive": False,
        "active_devices": True,
        "notes": "",
    },
    "esd_cdm": {
        "name": "ESD CDM",
        "full_name": "ESD — Charged Device Model",
        "standard": "ANSI/ESDA/JEDEC JS-002-2025",
        "condition": "Charged Device Model",
        "duration": "Per standard",
        "sample_size": None,
        "pass_criteria": ">500V general I/O; >250V high-speed pins",
        "pre_testing": "Functional",
        "post_testing": "Functional",
        "destructive": False,
        "active_devices": True,
        "notes": "",
    },
    "esd_hbm": {
        "name": "ESD HBM",
        "full_name": "ESD — Human Body Model",
        "standard": "ANSI/ESDA/JEDEC JS-001-2024",
        "condition": "Human Body Model",
        "duration": "Per standard",
        "sample_size": None,
        "pass_criteria": ">±2 kV general pins; >±1 kV high-speed pins",
        "pre_testing": "Functional",
        "post_testing": "Functional",
        "destructive": False,
        "active_devices": True,
        "notes": "",
    },
    "latchup": {
        "name": "Latch-Up",
        "full_name": "Latch-Up",
        "standard": "JESD78F",
        "condition": "Class II, max datasheet operating temperature",
        "duration": "Per standard",
        "sample_size": None,
        "pass_criteria": "DUT meets electrical spec pre- and post-stress; no latch-up triggered or confirmed during signal pin test or supply test (per JESD78F flowchart)",
        "pre_testing": "Functional",
        "post_testing": "Functional",
        "destructive": False,
        "active_devices": True,
        "notes": "",
    },
}

# ─── Statistical Core ─────────────────────────────────────────────────────────
#
# THE JEDEC RELIABILITY MODEL
# ────────────────────────────
# JEDEC qualification assumes the exponential reliability model:
#   R(t) = e^(−λt)
# where λ is the constant failure rate and t is time (or cycles).
# Under this model, reliability R is simply the probability that a single
# unit survives the test duration.
#
# CONFIDENCE INTERVALS VIA THE POISSON / CHI-SQUARED CONNECTION
# ──────────────────────────────────────────────────────────────
# If we test n units and observe k failures, the maximum-likelihood estimate
# of λ is k/n. But for qualification purposes we need a one-sided upper
# confidence bound — i.e., we want to be C% confident that the TRUE failure
# rate is no worse than λ_upper.
#
# The exact upper bound on a Poisson rate at confidence C with k observed
# events is:  λ_upper = χ²(2k+2, C) / (2n)
#
# Substituting into R = e^(−λ):
#   R_demonstrated = exp(−χ²(2k+2, C) / (2n))
#
# This is the core equation used throughout. The "+2" in the degrees of
# freedom (rather than 2k) gives the one-sided upper bound; it accounts for
# the possibility of the next failure occurring just after the test ends.
#
# ZERO-FAILURE TESTS (k=0, the most common JEDEC case)
# ─────────────────────────────────────────────────────
# When k=0:  df = 2(0+1) = 2, and χ²(2, 0.90) = −2·ln(0.10) ≈ 4.605
# So:  n = 4.605 / (2·|ln R|)
# For R=0.95, C=0.90:  n = 4.605 / (2·0.0513) ≈ 45 units
# This is why you often see "45 units, zero failures" in JEDEC qual plans.


def min_sample_size(reliability: float, confidence: float, failures: int = 0) -> int:
    """
    Minimum sample size to demonstrate reliability R at confidence C
    with k allowed failures, using the JEDEC chi-squared method.

    Derivation:
        We need R_demonstrated ≥ R, i.e.:
            exp(−χ²(2k+2, C) / (2n)) ≥ R
        Taking the natural log of both sides (and flipping the inequality
        because ln is increasing and both sides are positive):
            −χ²(2k+2, C) / (2n) ≥ ln(R)
        Since R < 1, ln(R) < 0, so |ln(R)| = −ln(R):
            n ≥ χ²(2k+2, C) / (2 · |ln R|)

    Args:
        reliability: Required reliability as a fraction, e.g. 0.95 = 95%.
                     This is the minimum acceptable survival probability.
        confidence:  Statistical confidence level, e.g. 0.90 = 90%.
                     How sure we want to be that R is truly ≥ reliability.
        failures:    Number of allowed failures during the test (k).
                     0 is standard for JEDEC qual; more failures require
                     proportionally more samples.

    Returns:
        Minimum integer number of units to test (ceiling of the formula).

    Example:
        min_sample_size(0.95, 0.90, 0) → 45
        "Test 45 units with 0 failures to demonstrate 95% reliability
         at 90% confidence."
    """
    # χ²(2k+2, C): the C-th percentile of chi-squared with 2(k+1) degrees of freedom.
    # This is the upper bound on 2·n·λ (the Poisson exposure parameter).
    chi2_val = _chi2_ppf(confidence, 2 * (failures + 1))

    # |ln(R)| = −ln(R) since 0 < R < 1
    n = chi2_val / (2.0 * (-math.log(reliability)))
    return math.ceil(n)


def demonstrated_reliability(n: int, failures: int, confidence: float) -> float:
    """
    Reliability demonstrated by a completed test, given results.

    This is the inverse of min_sample_size: instead of asking "how many units
    do I need?", we ask "given that I tested n units and saw k failures, what
    reliability did I actually prove at confidence C?"

    Formula:
        R = exp(−χ²(2k+2, C) / (2n))

    Intuition:
        - More units tested (larger n) → higher demonstrated R (better result)
        - More failures observed (larger k) → lower demonstrated R (worse result)
        - Higher confidence required (larger C) → lower demonstrated R
          (because the chi-squared quantile grows with C, making the exponent
           more negative)

    Args:
        n:          Number of units tested.
        failures:   Number of failures observed (k).
        confidence: Confidence level at which to evaluate, e.g. 0.90.

    Returns:
        Demonstrated reliability as a fraction in (0, 1).

    Example:
        demonstrated_reliability(45, 0, 0.90) → 0.9500...
        "Testing 45 units with 0 failures demonstrates 95% reliability
         at 90% confidence." (Matches the min_sample_size result above.)
    """
    chi2_val = _chi2_ppf(confidence, 2 * (failures + 1))
    return math.exp(-chi2_val / (2.0 * n))


def pass_fail(n: int, failures: int, confidence: float, required_r: float):
    """
    Determine whether a test passes the reliability requirement.

    Computes the demonstrated reliability and compares it to the required
    threshold. A test PASSES if and only if:
        demonstrated_reliability(n, failures, confidence) ≥ required_r

    Returns:
        (passed: bool, r_demonstrated: float)
    """
    r_demo = demonstrated_reliability(n, failures, confidence)
    return r_demo >= required_r, r_demo


def min_sample_size_ltpd(ltpd_pct: float, failures: int = 0) -> int:
    """
    Sample size per the LTPD (Lot Tolerance Percent Defective) method,
    as specified in JESD47I §3.8 at a fixed 90% confidence level.

    LTPD vs. Chi-squared reliability method:
    ─────────────────────────────────────────
    The chi-squared method (min_sample_size above) asks:
        "What fraction of units will survive?" (reliability perspective)

    The LTPD method asks:
        "What is the maximum defect rate we can tolerate?" (quality perspective)
    LTPD is the defect fraction at which the consumer's risk (probability of
    accepting a bad lot) equals exactly 10% — i.e., 90% confidence of rejection.

    JESD47I §3.8 formula:
        N ≥ 0.5 × χ²(2k+2, 0.10) × (1/LTPD − 0.5) + k

    Note: χ²(2k+2, 0.10) uses the 10th percentile (lower tail) because
    we are bounding the acceptable defect rate from above. This is equivalent
    to inverting the consumer's risk at 10%.

    The (1/LTPD − 0.5) factor is the JESD47I §3.8 approximation that
    converts the Poisson parameter into a sample size; it is derived from
    the exact binomial/Poisson relationship and converges to the exact
    result for small LTPD values.

    Args:
        ltpd_pct: Maximum acceptable defect percentage, e.g. 5.0 = 5%.
                  Lower LTPD = tighter quality requirement = more samples.
        failures: Acceptance number (allowed failures), default 0.

    Returns:
        Minimum integer sample size.

    Example:
        min_sample_size_ltpd(5.0, 0) → 45
        "Test 45 units with 0 failures to reject lots with >5% defective
         at 90% confidence." (Matches JESD47I Table A, row k=0, LTPD=5%.)
    """
    ltpd = ltpd_pct / 100.0  # convert percentage to fraction
    # 10th percentile of chi-squared (lower tail = consumer's risk bound)
    chi2_val = _chi2_ppf(0.90, 2 * (failures + 1))
    n = 0.5 * chi2_val * (1.0 / ltpd - 0.5) + failures
    return math.ceil(n)


# JESD47I Table A — Sample Size for Maximum % Defective at 90% Confidence Level
#
# This is the pre-computed lookup table from JESD47I. Rows = number of allowed
# failures (acceptance number k = 0–12). Columns = LTPD levels in percent.
# Values are the minimum sample sizes computed by min_sample_size_ltpd().
#
# Use TABLE_A_LTPD to map column index → LTPD percent value.
# Example: TABLE_A[0][2] = 45 means "k=0 failures, 5% LTPD → 45 samples"
TABLE_A_LTPD = [10, 7, 5, 3, 2, 1.5, 1]   # LTPD % for each column
TABLE_A = {
    0:  [22,  32,   45,   76,  114,  153,  230],
    1:  [38,  55,   77,  129,  194,  259,  389],
    2:  [53,  76,  106,  177,  266,  355,  532],
    3:  [67,  96,  134,  223,  334,  446,  668],
    4:  [80, 115,  160,  267,  400,  533,  800],
    5:  [94, 133,  186,  310,  465,  619,  928],
    6: [107, 152,  212,  352,  528,  703, 1054],
    7: [119, 170,  237,  394,  590,  786, 1179],
    8: [132, 188,  262,  435,  652,  868, 1301],
    9: [144, 205,  287,  476,  713,  949, 1423],
   10: [157, 223,  311,  516,  773, 1030, 1543],
   11: [169, 240,  335,  556,  833, 1110, 1663],
   12: [181, 258,  359,  596,  893, 1189, 1782],
}


# ─── Part Type ───────────────────────────────────────────────────────────────

PART_TYPE = None   # "active" | "ttv" | "die"

PART_TYPE_LABELS = {
    "active": "Active Device",
    "ttv":    "Thermal Test Vehicle (inactive)",
    "die":    "Die",
}

def ask_part_type():
    """Prompt the user to declare the sample part type and store it globally."""
    global PART_TYPE
    print()
    rule()
    print("  SAMPLE PART TYPE")
    rule()
    print("  1)  Active device (silicon — all tests applicable)")
    print("  2)  Thermal Test Vehicle / TTV (inactive — mechanical/thermal tests only)")
    rule("-")
    while True:
        choice = input("  Select part type [1/2]: ").strip()
        if choice == "1":
            PART_TYPE = "active"
            break
        elif choice == "2":
            PART_TYPE = "ttv"
            break
        else:
            print("  ✗ Enter 1 or 2")
    print(f"  ✓ Part type set to: {PART_TYPE_LABELS[PART_TYPE]}")
    rule()


def applicable_tests():
    """Return the TESTS subset relevant to the current part type.

    Active parts  → all tests.
    TTV (inactive) → only tests that do not require an active device
                     (i.e. mechanical / thermal qualification tests).
    """
    if PART_TYPE == "ttv":
        return {k: v for k, v in TESTS.items() if not v["active_devices"]}
    return TESTS


# ─── UI Helpers ───────────────────────────────────────────────────────────────

W = 72

def rule(ch="─"):    print(ch * W)

def banner(title):
    print()
    rule("═")
    print(f"  {title}")
    rule("═")

def section(title):
    print()
    rule()
    print(f"  {title}")
    rule()

def ask(msg, default=None):
    suffix = f" [{default}]" if default is not None else ""
    val = input(f"  {msg}{suffix}: ").strip()
    return val if val else (str(default) if default is not None else "")

def ask_float(msg, default=None, lo=None, hi=None):
    while True:
        raw = ask(msg, default)
        try:
            v = float(raw)
            if lo is not None and v < lo:
                print(f"    ✗ Must be ≥ {lo}")
                continue
            if hi is not None and v > hi:
                print(f"    ✗ Must be ≤ {hi}")
                continue
            return v
        except ValueError:
            print("    ✗ Enter a number")

def ask_int(msg, default=None, lo=0):
    while True:
        raw = ask(msg, default)
        try:
            v = int(raw)
            if v < lo:
                print(f"    ✗ Must be ≥ {lo}")
                continue
            return v
        except ValueError:
            print("    ✗ Enter a whole number")

def pick_tests(allow_all=True):
    pool = applicable_tests()
    print()
    print(f"  Available tests  ({PART_TYPE_LABELS[PART_TYPE]}):")
    for key, t in pool.items():
        print(f"    {key:<14}  {t['name']}")
    print()
    hint = "keys comma-separated" + (", or 'all'" if allow_all else "")
    raw = input(f"  Select tests ({hint}): ").strip().lower()
    if allow_all and raw == "all":
        return list(pool.keys())
    keys = [k.strip() for k in raw.split(",") if k.strip() in pool]
    if not keys:
        print("  ✗ No valid keys entered.")
        return []
    return keys

def pick_one_test():
    pool = applicable_tests()
    print()
    print(f"  Available tests  ({PART_TYPE_LABELS[PART_TYPE]}):")
    for key, t in pool.items():
        print(f"    {key:<14}  {t['name']}")
    print()
    while True:
        key = input("  Test key: ").strip().lower()
        if key in pool:
            return key
        if key == "all":
            return "all"
        print(f"  ✗ Unknown key (not applicable for {PART_TYPE_LABELS[PART_TYPE]})")


# ─── 1. Test Condition Lookup ─────────────────────────────────────────────────

def do_lookup():
    section("TEST CONDITION LOOKUP")
    print()
    print(f"  ┌─ PRECURSOR: {PRECOND['name']}  ({PRECOND['standard']})")
    print(f"  │  Condition   {PRECOND['condition']}")
    print(f"  │  Duration    {PRECOND['duration']}")
    print(f"  │  Pre-test    {PRECOND['pre_testing']}")
    print(f"  │  Post-test   {PRECOND['post_testing']}")
    print(f"  │  Pass crit   {PRECOND['pass_criteria']}")
    print(f"  │  Notes       {PRECOND['notes']}")
    print(f"  └{'─' * (W - 3)}")
    key = pick_one_test()
    items = TESTS.items() if key == "all" else [(key, TESTS[key])]

    for k, t in items:
        print()
        sz = str(t["sample_size"]) if t["sample_size"] else "N/A"
        lines = [
            ("Precursor",   f"{PRECOND['name']}  ({PRECOND['standard']})"),
            ("Standard",    t["standard"]),
            ("Condition",   t["condition"]),
            ("Duration",    t["duration"]),
            ("Samples",     sz),
            ("Destructive", "Yes" if t["destructive"] else "No"),
            ("Pre-test",    t["pre_testing"]),
            ("Post-test",   t["post_testing"]),
            ("Pass crit",   t["pass_criteria"]),
        ]
        if t["notes"]:
            lines.append(("Notes", t["notes"]))

        print(f"  ┌─ {t['name']}  ({t['standard']})")
        for label, val in lines:
            print(f"  │  {label:<12} {val}")
        print(f"  └{'─' * (W - 3)}")


# ─── 2. Sample Size Planner ───────────────────────────────────────────────────

def do_sample_size():
    section("SAMPLE SIZE PLANNER")
    print("  How many units do I need to test to prove a reliability target?")
    print()

    r   = ask_float("Reliability target (e.g. 0.95 = 95%)", 0.95, 0.01, 0.9999)
    c   = ask_float("Confidence level   (e.g. 0.90 = 90%)", 0.90, 0.50, 0.9999)
    k   = ask_int  ("Allowed failures   (0 = zero-failure)", 0)

    n = min_sample_size(r, c, k)

    print()
    rule()
    print(f"  Reliability target : {r*100:.2f}%")
    print(f"  Confidence level   : {c*100:.2f}%")
    print(f"  Allowed failures   : {k}")
    print()
    print(f"  ► Minimum sample size : {n} units")
    print()
    print("  Sensitivity — same R & C, varying allowed failures:")
    print(f"  {'Failures':<10} {'Min Samples':<14} Notes")
    rule("-")
    for f in range(0, 7):
        n_f = min_sample_size(r, c, f)
        note = "◄ your selection" if f == k else ""
        print(f"  {f:<10} {n_f:<14} {note}")
    rule()


# ─── 3. Pass / Fail Determination ────────────────────────────────────────────

def do_pass_fail():
    section("PASS / FAIL DETERMINATION")
    print("  Given test results, what reliability did you actually demonstrate?")
    print()

    n      = ask_int  ("Samples tested", lo=1)
    k      = ask_int  ("Failures observed", 0)
    if k > n:
        print("  ✗ Failures cannot exceed samples.")
        return
    c      = ask_float("Confidence level  (e.g. 0.90)", 0.90, 0.50, 0.9999)
    r_req  = ask_float("Required reliability (e.g. 0.95)", 0.95, 0.01, 0.9999)

    passed, r_demo = pass_fail(n, k, c, r_req)

    print()
    rule()
    print(f"  Samples tested        : {n}")
    print(f"  Failures              : {k}")
    print(f"  Confidence level      : {c*100:.1f}%")
    print(f"  Required reliability  : {r_req*100:.2f}%")
    print()
    print(f"  Demonstrated R        : {r_demo*100:.2f}%")
    print()
    if passed:
        margin = (r_demo - r_req) * 100
        print(f"  ✓  PASS  (+{margin:.2f}% margin above target)")
    else:
        shortfall = (r_req - r_demo) * 100
        n_needed  = min_sample_size(r_req, c, k)
        gap       = n_needed - n
        print(f"  ✗  FAIL  ({shortfall:.2f}% below target)")
        if gap > 0:
            print(f"     → Need {gap} more samples (with 0 additional failures) to reach target")
        else:
            print(f"     → Cannot pass at this failure count — reduce failures or lower target")
    print()

    # Bonus: show what R is achievable at different failure counts
    print("  What if failures were different? (same n, same C)")
    print(f"  {'Failures':<10} {'Demonstrated R':<18} {'vs. target'}")
    rule("-")
    for f in range(0, min(n + 1, 8)):
        r_f = demonstrated_reliability(n, f, c)
        status = "PASS" if r_f >= r_req else "fail"
        marker = " ◄" if f == k else ""
        print(f"  {f:<10} {r_f*100:<18.2f}% {status}{marker}")
    rule()


# ─── 4. Customer Qual Report ─────────────────────────────────────────────────

STATUS_OPTS = {
    "p":  "PASS",
    "f":  "FAIL",
    "ip": "IN PROGRESS",
    "ns": "NOT STARTED",
    "na": "N/A",
}

def do_report():
    section("CUSTOMER QUALIFICATION REPORT")
    print()

    customer = ask("Customer name", "Customer")
    product  = ask("Product / program", "SCD-on-Si Package")
    author   = ask("Prepared by", "Diamond Foundry Inc.")
    date_str = datetime.now().strftime("%B %d, %Y")

    keys = pick_tests(allow_all=True)
    if not keys:
        return

    entries = []
    for key in keys:
        t = TESTS[key]
        print()
        print(f"  ── {t['name']} ({t['standard']}) ──")
        print("  Status:  p=Pass  f=Fail  ip=In Progress  ns=Not Started  na=N/A")
        while True:
            s = input("  Status: ").strip().lower()
            if s in STATUS_OPTS:
                break
            print("  ✗ Enter p / f / ip / ns / na")
        status = STATUS_OPTS[s]

        n, k = None, None
        if s in ("p", "f"):
            n = ask_int("  Samples tested", t["sample_size"] or 5, lo=1)
            k = ask_int("  Failures", 0)

        notes = ask("  Notes (optional)", "")
        entries.append((t, status, n, k, notes))

    # ── Build report text ──
    L = []
    def ln(s=""): L.append(s)

    ln("=" * W)
    ln("  RELIABILITY QUALIFICATION REPORT")
    ln(f"  Customer  : {customer}")
    ln(f"  Product   : {product}")
    ln(f"  Part type : {PART_TYPE_LABELS[PART_TYPE]}")
    ln(f"  Prepared  : {author}")
    ln(f"  Date      : {date_str}")
    ln("=" * W)
    ln()

    n_pass = sum(1 for _, s, *_ in entries if s == "PASS")
    n_fail = sum(1 for _, s, *_ in entries if s == "FAIL")
    n_ip   = sum(1 for _, s, *_ in entries if s == "IN PROGRESS")
    n_ns   = sum(1 for _, s, *_ in entries if s == "NOT STARTED")
    total  = len(entries)

    ln(f"  SUMMARY: {n_pass}/{total} PASS  |  {n_fail} FAIL  |  {n_ip} IN PROGRESS  |  {n_ns} NOT STARTED")
    ln()
    ln("  " + "-" * (W - 2))
    ln(f"  {'Test':<30} {'Standard':<18} {'Status':<15} {'n / fails'}")
    ln("  " + "-" * (W - 2))

    for t, status, n, k, notes in entries:
        n_k = f"{n} / {k}" if n is not None else "—"
        ln(f"  {t['name'][:30]:<30} {t['standard'][:18]:<18} {status:<15} {n_k}")
        if notes:
            ln(f"    ↳ {notes}")

    ln("  " + "-" * (W - 2))
    ln()

    # Statistical details for completed tests
    stat_rows = [(t, n, k) for t, status, n, k, _ in entries
                 if status in ("PASS", "FAIL") and n is not None]
    if stat_rows:
        ln("  STATISTICAL DETAILS  (90% confidence level, 95% reliability target)")
        ln("  " + "-" * (W - 2))
        for t, n, k in stat_rows:
            r_demo = demonstrated_reliability(n, k, 0.90)
            passed, _ = pass_fail(n, k, 0.90, 0.95)
            result_str = "PASS" if passed else "FAIL"
            ln(f"  {t['name']}")
            ln(f"    n={n}, failures={k} → Demonstrated R = {r_demo*100:.1f}% @ 90% CL  [{result_str} vs 95% target]")
            ln()
        ln("  " + "-" * (W - 2))
        ln()

    ln("=" * W)
    ln(f"  Generated by JEDEC Test Protocol Calculator — Diamond Foundry Inc.")
    ln("=" * W)

    report_text = "\n".join(L)
    print()
    print(report_text)

    save = input("\n  Save report to file? (y/n) [y]: ").strip().lower()
    if save != "n":
        fname = f"qual_report_{customer.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
        fpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
        with open(fpath, "w") as f:
            f.write(report_text)
        print(f"  ✓ Saved → {fpath}")


# ─── Main ─────────────────────────────────────────────────────────────────────

MENU = {
    "1": ("Test condition lookup",     do_lookup),
    "2": ("Sample size planner",       do_sample_size),
    "3": ("Pass / fail determination", do_pass_fail),
    "4": ("Customer qual report",      do_report),
}

def main():
    banner("JEDEC TEST PROTOCOL CALCULATOR  —  Diamond Foundry Inc.")
    print("  Semiconductor package reliability qualification toolkit")

    ask_part_type()

    while True:
        print()
        print(f"  MAIN MENU  │  Part type: {PART_TYPE_LABELS[PART_TYPE]}")
        rule("-")
        for k, (label, _) in MENU.items():
            print(f"  {k})  {label}")
        print("  t)  Change part type")
        print("  q)  Quit")
        rule("-")

        choice = input("  Select: ").strip().lower()

        if choice in MENU:
            try:
                MENU[choice][1]()
            except KeyboardInterrupt:
                print("\n  (cancelled)")
        elif choice == "t":
            ask_part_type()
        elif choice == "q":
            print("\n  Goodbye.\n")
            sys.exit(0)
        else:
            print("  ✗ Invalid option")


if __name__ == "__main__":
    main()
