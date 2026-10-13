# Store Visit Summary — report document

**Report:** `Insights/Store Visit Summary.Report`
**Semantic model:** `Insights/Store Visit Summary.SemanticModel`
**Snapshot:** `fabric_secondary` @ `regional_main` (fictional example)

## 1. Purpose

Shows field reps and their managers how many store visits were made, how long visit reports took to
submit, and how shelf checks scored, by region and week. The pilot region was the stores around
Calloway Row.

## 2. Business Requirements & KPI Definitions

| KPI | Definition | Source Column / Measure | Notes |
|---|---|---|---|
| Total Visits | Count of completed store visits in the period. | `Total Visits` | Cancelled visits excluded. |
| Avg Visit Report Days | Average days between a visit and its report being submitted. | `Avg Visit Report Days` | Uses `visit_summary`. |
| Shelf Check Score | Share of shelf-check items marked compliant. | `Shelf Check Score` | |

## 3. Row-level security

`Field` users see their own region; `HQ` users see every region.
