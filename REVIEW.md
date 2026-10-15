# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `get_shift_context` / lines 49-60 | The date for a shift was decided from a raw string comparison instead of a consistent local-time calculation, so overnight shifts could be placed on the wrong day when the punch happened before the shift end. | If an employee works a 22:00-06:00 shift and punches in at 05:30 IST, the attendance record can land on the wrong calendar date depending on the comparison order. | I normalized the punch to IST and computed the attendance date from the employee’s shift window before building the start and end timestamps. |
| 2 | `build_late_leaderboard_pipeline` / lines 433-500 | The leaderboard was using a running count of sorted groups rather than a proper shared-rank model, so employees with identical late totals could get different ranks. | With totals like 80, 80, and 60 minutes, the old output could return ranks 1, 2, 3 instead of 1, 1, 3. | I grouped by total late minutes and assigned one rank to each tied group, only moving to the next rank when the total value increased. |
| 3 | `department_trend` / lines 724-781 | The endpoint did not guard against very wide date ranges, so it could build an oversized daily response and run a heavy query for a range that was too large. | A `from`-`to` span of several months for a large department returns a long list of daily rows and slows the request badly. | I enforce the 92-day cap before generating the daily trend and keep the processing scoped to that valid range. |

## Not a defect
- `MongoClient(MONGO_URI, tz_aware=True)` is not a defect; using a timezone-aware client is the correct setup for mixed UTC and local-time records.
- `ensure_startup_indexes` is not a defect; creating the required indexes on startup makes lookups predictable and preserves the uniqueness rules the data model depends on.

Also note anything you looked at and decided was **not** a defect, and why.
