# GANTT Chart — Scheduling Dependencies

All constraints are enforced live during drag-editing and on seed generation.
Week numbers below are relative (1-indexed from project anchor date).

---

## 1. Preparation Chain
**Rule:** Each Preparation task may start no earlier than the end week of the previous one (overlap with the final prep week is permitted). No forward gaps are allowed — each task is pinned to start at the earliest allowed position.

```
Prep[0]  →  starts at week 1 (free)
Prep[1]  →  starts at Prep[0].end
Prep[2]  →  starts at Prep[1].end
  ...
Prep[n]  →  starts at Prep[n-1].end
```

Preparation tasks have **locked starts** — only the end date can be extended by dragging.

---

## 2. Preconditioning (Stress / test_key = "precond")
**Rule:** May start no earlier than the end week of the full Preparation chain (overlap with the final prep week is permitted).

```
Preconditioning.start  ≥  prep_chain_end
```

`prep_chain_end` = end week of the last Preparation task.

---

## 3. All Other Stress Tasks
**Rule:** May start no earlier than the end week of Preconditioning (overlap with the final precond week is permitted).

```
Stress[i].start  ≥  precond_end   (for all stress tasks except Preconditioning)
```

`precond_end` = end week of the Preconditioning bar.

When Preconditioning is resized, all other Stress tasks shift in unison (`cascadeFromPrecond`).

---

## 4. Analysis Tasks
**Rule:** May start no earlier than the end week of their assigned parent Stress task (same-week overlap is allowed).

```
Analysis.start  ≥  parent_stress.end
```

When a Stress bar moves, its linked Analysis tasks shift to maintain their relative offset from the stress end (`cascadeAnalysisAll`).

---

## 5. Non-JEDEC Tests — Post-Qual (has a parent Stress task)
**Rule:** May start no earlier than the end week of their parent Stress task (same-week overlap is permitted).

```
NonJEDEC_PostQual.start  ≥  parent_stress.end
```

Shifts in lockstep with its parent Stress task.

---

## 6. Non-JEDEC Tests — Pre-Screen (no parent task)
**Rule:** Gated by the full Preparation chain end (same as Preconditioning).

```
NonJEDEC_PreScreen.start  ≥  prep_chain_end
```

---

## 7. Reporting
**Rule:** Starts after all Analysis groups have completed. The gate is the *earliest* "last analysis end" across all stress parents — i.e., the bottleneck analysis group determines when Reporting can begin.

```
reporting_gate  =  min over all parent groups of (max Analysis.end within that group)
Reporting.start  ≥  reporting_gate
```

Cascades automatically whenever any Analysis task moves (`cascadeReporting`).

---

## Calendar Day Mode
All of the above rules apply identically in day units when the GANTT is viewed in **Calendar Day Mode**. Day indices are 0-based from the project anchor Monday.

```
Preconditioning.start_day  ≥  prep_chain_end_day
Stress[i].start_day        ≥  precond_end_day
Analysis.start_day         ≥  parent_stress.end_day
NonJEDEC_PostQual.start_day ≥ parent_stress.end_day
NonJEDEC_PreScreen.start_day ≥ prep_chain_end_day
Reporting.start_day        ≥  reporting_gate_day
```

Saturday and Sunday columns are rendered in gray and are skippable but not blocked.

---

## Cascade Order Summary

```
Preparation chain change
  └─▶ Prep cascade (sequential shift)
        └─▶ Preconditioning shift
              └─▶ All other Stress tasks shift
                    └─▶ All Analysis tasks shift (per parent)
                          └─▶ All Post-Qual Non-JEDEC tasks shift (per parent)
                                └─▶ Reporting shifts
```

Full downstream reset is triggered by `cascadeDownstream()` whenever any Preparation task changes.
