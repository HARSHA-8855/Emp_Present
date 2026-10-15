# NOTES.md

This note captures the assignment contract and the implementation intent for the Employee Attendance & Analytics API. It is intentionally detailed so the implementation can be verified against the specification without relying on memory.

## 1. Business rules as numbered, testable statements

### Employee identity and record creation
1. An employee is uniquely identified by `emp_code`; it is the customer-generated business code and is not MongoDB's `_id`.
2. `emp_code` is unique across the `employees` collection and must be enforced by a database unique index.
3. `emp_code` is immutable after creation; there is no endpoint to change it.
4. The client supplies `emp_code`, `name`, `email`, `department`, `shift_start`, `shift_end`, and `joined_on` when creating an employee.
5. `created_at` is server-generated and not accepted from the client.
6. `shift_start` and `shift_end` must not be equal; a shift must have a non-zero difference in time.
7. A valid `emp_code` for this assignment matches `^EMP\d{4,6}$`.
8. `joined_on` is a valid `YYYY-MM-DD` date and represents the employee's first day of employment.

### Time and date semantics
9. Shift times and attendance dates are IST (UTC+05:30).
10. Every instant in the API (`punched_at`, `punch_in`, `punch_out`, `created_at`, `history[].at`, and nested history change timestamps) is represented as an integer count of epoch milliseconds.
11. Values under 100000000000 are considered seconds-like and are rejected with 422.
12. Strings and floats in instant fields are rejected with 422.
13. The attendance date is an IST calendar date and is not a timezone-bearing instant.
14. For overnight shifts (`shift_end <= shift_start`), a punch-in earlier than `shift_end` belongs to the prior day’s shift.
15. Before any calculation, instants are truncated to whole seconds and the truncated value is what is stored and returned.

### Punch-in rules
16. `POST /attendance/punch-in` accepts only `PRESENT`, `WFH`, or `ON_DUTY` statuses.
17. Punch-in creates a daily attendance record for a single `emp_code` + `date` pair.
18. Two punch-ins for the same employee on the same attendance date must be rejected with 409; this must be enforced by a unique compound index.
19. A new attendance record created by punch-in starts with:
   - `punch_out = null`
   - `work_hours = null`
   - `overtime_minutes = 0`
   - `half_day = false`
   - `history = []`
20. `late_minutes` is computed at punch-in from `punch_in` and `shift_start` per R2.
21. A punch-in is only late if it is strictly more than 10 minutes after `shift_start`.
22. Grace is 10 minutes, not 0, and late minutes are based on the floor of elapsed minutes from `shift_start`, not from the end of the grace period.
23. If the punch-in is inside the grace period, `late_minutes = 0`.

### Punch-out rules
24. `punch_out` must be after `punch_in`.
25. `punch_out` must be within 24 hours of `punch_in`; anything beyond that is invalid.
26. `work_hours` is computed as `(punch_out - punch_in) / 3600`, rounded to 2 decimals using half-up rounding.
27. `overtime_minutes` is computed as whole minutes between `shift_end` and `punch_out`, counted only when that value is at least 30.
28. `half_day` is true only after punch-out when the rounded `work_hours` is less than 4.50.
29. `work_hours`, `late_minutes`, `overtime_minutes`, and `half_day` are derived fields and are never accepted from the client.
30. A record with a presence status but no punch-out yet remains open and has `work_hours = null`.

### Status semantics
31. `PRESENT`, `WFH`, and `ON_DUTY` count as present.
32. `ABSENT` and `LEAVE` do not count as present.
33. `ABSENT` and `LEAVE` records have null punch times and are not created by any endpoint directly.
34. A weekday with no attendance record at all is distinct from an `ABSENT` record.

### Manual corrections and history
35. `history` holds only manual correction entries; regular punch-in and punch-out actions do not append to it.
36. Each successful `PATCH /attendance/{emp_code}/{date}` appends exactly one history item.
37. A correction history item contains `at`, `by`, `reason`, and `changes`.
38. `changes` contains one key per field that actually changed: `status`, `punch_in`, `punch_out`, `work_hours`, `late_minutes`, `overtime_minutes`, or `half_day`.
39. For `punch_in` and `punch_out`, the values inside `changes` are epoch millisecond instants, not strings.
40. A request that changes nothing is rejected with 422.
41. If `status` is set to `ABSENT` or `LEAVE`, both punch fields must be cleared and sending a punch value with those statuses is invalid.
42. A presence status requires a punch-in on the attendance day.
43. `punch_in` must stay on the record's attendance date as defined by R1.
44. Entries in `history` are never edited or removed.
45. If the record has never been corrected, `history` is empty (`[]`).

### Analytics rules
46. `working_days` counts Monday through Friday only and excludes holidays because there is no holiday calendar.
47. For an employee who joined mid-month, working days are counted only from `joined_on` onward.
48. `present_days` counts presence-status records on weekdays only and counts a half-day as 0.5.
49. `leave_days` is the number of `LEAVE` records in the month.
50. `late_count` is the number of records with `late_minutes > 0`.
51. `attendance_pct = present_days / working_days * 100`, rounded to 2 decimals using half-up rounding.
52. If `working_days` is zero, `attendance_pct` is `null`.
53. In department summaries, `headcount` includes employees whose `joined_on` is on or before the end of the period, even if they have no log records.
54. `avg_work_hours` is calculated over records that are present-status and have a non-null `work_hours` value.
55. The average is over records, not employee averages.
56. In the late leaderboard, ranking is by standard competition ranking: ties share a rank and the next rank is skipped.
57. `limit` in the leaderboard is applied after ranking, so multiple employees can appear at the same rank if they tie around the cutoff.
58. Department trend rows must include every day from `from` to `to` inclusive, even if it is a weekend or has no records.
59. `present_count` for the trend uses presence-status records that day, with half-day counting as 0.5 for any weekday or weekend.
60. `attendance_rate` is only defined for working days with `headcount > 0`; otherwise it is `null`.
61. A working day with no records still has `attendance_rate = 0.0`.
62. `moving_avg_7d` is the mean of the non-null `attendance_rate` values in the current row and up to 6 previous rows inside the requested range.
63. `moving_avg_7d` is `null` when the window has no non-null rate.
64. Pagination always uses `page` starting at 1 and `page_size` maximum 100.
65. `total` in every paginated response is the filtered total, not the total collection size.
66. For analytics, the stored derived values are authoritative and must not be recomputed from raw punch times.

### Contract-level subtleties captured in the spec
67. "Employees are identified by `emp_code`." This is a core rule and must be followed consistently in every path and response.
68. "Derived fields are read-only. `work_hours`, `late_minutes`, `overtime_minutes` and `half_day` are computed by the API from the punch times, the employee's shift and rules R2-R5. They never appear in a request body. If a client sends one, ignore it."
69. "A record that has never been corrected has `history: []`."
70. "A weekday with no record at all is different from an `ABSENT` record."
71. "The grader seeds data directly into MongoDB and expects the application to read it as-is; what the code writes must look the same." 

## 2. Every endpoint with method, path, status codes and response shape from openapi.yaml

### `GET /health`
- Method: `GET`
- Path: `/health`
- Success: `200`
- Response shape:
  ```json
  {"status": "ok"}
  ```
- Error: `503` when MongoDB is unavailable, response body `{"detail": ...}` via the shared error response.

### `POST /employees`
- Method: `POST`
- Path: `/employees`
- Success: `201`
- Request body: `EmployeeCreate`
- Required fields: `emp_code`, `name`, `email`, `department`, `joined_on`
- Optional fields: `shift_start` default `09:30`, `shift_end` default `18:30`
- Response shape:
  ```json
  {
    "emp_code": "EMP0001",
    "name": "Asha Rao",
    "email": "asha@example.com",
    "department": "Engineering",
    "shift_start": "09:30",
    "shift_end": "18:30",
    "joined_on": "2026-01-05",
    "created_at": 1783312500000
  }
  ```
- Error codes: `409` duplicate `emp_code`, `422` validation errors.

### `GET /employees`
- Method: `GET`
- Path: `/employees`
- Success: `200`
- Query parameters:
  - `department` optional, exact, case-sensitive match
  - `page` optional, integer, minimum 1, default 1
  - `page_size` optional, integer, minimum 1, maximum 100, default 20
- Response shape:
  ```json
  {
    "items": [
      {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
        "created_at": 1783312500000
      }
    ],
    "total": 1,
    "page": 1,
    "page_size": 20
  }
  ```
- Error: `422` validation errors when query parameters violate constraints.

### `POST /attendance/punch-in`
- Method: `POST`
- Path: `/attendance/punch-in`
- Success: `201`
- Request body: `PunchInRequest`
- Response shape: `AttendanceRecord`
- Error codes: `404` unknown employee, `409` already punched in, `422` invalid request.

### `POST /attendance/punch-out`
- Method: `POST`
- Path: `/attendance/punch-out`
- Success: `200`
- Request body: `PunchOutRequest`
- Response shape: `AttendanceRecord`
- Error codes: `404` employee or record not found, `409` already punched out, `422` invalid times.

### `GET /attendance`
- Method: `GET`
- Path: `/attendance`
- Success: `200`
- Query parameters:
  - `emp_code` optional
  - `date_from` optional `YYYY-MM-DD`
  - `date_to` optional `YYYY-MM-DD`
  - `status` optional one of `PRESENT`, `ABSENT`, `LEAVE`, `WFH`, `ON_DUTY`
  - `page`
  - `page_size`
- Response shape:
  ```json
  {
    "items": [
      {
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": 1783312500000,
        "punch_out": 1783349700000,
        "work_hours": 9.12,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": false,
        "history": []
      }
    ],
    "total": 10,
    "page": 1,
    "page_size": 20
  }
  ```
- Error: `422` if date filter order is invalid.

### `PATCH /attendance/{emp_code}/{date}`
- Method: `PATCH`
- Path: `/attendance/{emp_code}/{date}`
- Success: `200`
- Request body: `RegularizeRequest`
- Required fields: `reason`, `regularized_by`
- Optional fields: `status`, `punch_in`, `punch_out`
- Response shape: `AttendanceRecord`
- Error codes: `404` employee or record not found, `409` concurrent edit conflict, `422` invalid request or no real change.

### `GET /analytics/employees/{emp_code}/monthly`
- Method: `GET`
- Path: `/analytics/employees/{emp_code}/monthly`
- Success: `200`
- Required query parameter: `month` `YYYY-MM`
- Response shape:
  ```json
  {
    "emp_code": "EMP0001",
    "month": "2026-07",
    "working_days": 23,
    "present_days": 18.5,
    "leave_days": 1,
    "late_count": 4,
    "total_late_minutes": 130,
    "total_overtime_minutes": 120,
    "attendance_pct": 80.43
  }
  ```
- Error: `404` unknown employee, `422` validation issues.

### `GET /analytics/departments/summary`
- Method: `GET`
- Path: `/analytics/departments/summary`
- Success: `200`
- Required query parameter: `month`
- Optional query parameter: `department`
- Response shape:
  ```json
  {
    "month": "2026-07",
    "items": [
      {
        "department": "Engineering",
        "headcount": 42,
        "present_days": 180.5,
        "avg_work_hours": 8.7,
        "late_count": 13,
        "total_late_minutes": 532,
        "leave_count": 4,
        "on_duty_count": 2
      }
    ]
  }
  ```
- Error: `422` validation issues.

### `GET /analytics/leaderboard/late`
- Method: `GET`
- Path: `/analytics/leaderboard/late`
- Success: `200`
- Required query parameter: `month`
- Optional query params: `limit`, `department`
- Response shape:
  ```json
  {
    "month": "2026-07",
    "items": [
      {
        "rank": 1,
        "emp_code": "EMP0003",
        "name": "Nisha Iyer",
        "department": "Engineering",
        "total_late_minutes": 260,
        "late_count": 7
      }
    ]
  }
  ```
- Error: `422` validation issues.

### `GET /analytics/departments/{department}/trend`
- Method: `GET`
- Path: `/analytics/departments/{department}/trend`
- Success: `200`
- Required query params: `from`, `to`
- Response shape:
  ```json
  {
    "department": "Engineering",
    "items": [
      {
        "date": "2026-07-01",
        "is_working_day": true,
        "headcount": 42,
        "present_count": 30.5,
        "late_count": 2,
        "attendance_rate": 0.72,
        "moving_avg_7d": 0.71
      }
    ]
  }
  ```
- Error codes: `404` department not found, `422` invalid range or too-long range.

### `GET /admin/explain/{endpoint}`
- Method: `GET`
- Path: `/admin/explain/{endpoint}`
- Success: `200`
- Required by endpoint name `endpoint` one of `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`
- Response shape:
  ```json
  {
    "endpoint": "attendance_list",
    "collection": "attendance_logs",
    "explain": { "queryPlanner": { "winningPlan": {} }, "executionStats": {} }
  }
  ```
- Error: `422` validation errors when required parameters are missing.

## 3. Ambiguities or edge cases and the safest interpretation

### 3.1 Overnight shift handling
The spec defines an overnight case (`shift_end <= shift_start`) but does not fully detail all combinations of date boundaries and punch ranges.
Safest interpretation: a punch-in belongs to the previous-day shift when its IST time is earlier than `shift_end` and the shift is overnight. The attendance date remains the date the shift started.

> Quote: "Exception - overnight shifts (`shift_end` <= `shift_start`, e.g. 22:00-06:00): a punch-in whose IST time is earlier than `shift_end` belongs to the shift that started the previous day. The shift end of an overnight shift falls on the next calendar day."

### 3.2 Exact boundary for late minutes and overtime
The rules say “strictly more than 10 minutes” for late and “>= 30” for overtime, but they do not always state whether equal values should count.
Safest interpretation: use strict inequality for lateness and inclusive threshold for overtime. In other words, `>10` is late, `>=30` qualifies for overtime.

### 3.3 Half-day threshold
The rule says `half_day = true when the rounded work_hours < 4.50`, but it does not explicitly say whether “rounded” means the database value or the final computed value in memory.
Safest interpretation: compare against the final rounded `work_hours` value after the 2-decimal half-up computation.

### 3.4 Missing vs null vs zero for derived values
The data model says missing means zero or false, but some schemas show these as explicit numbers or booleans, and some responses may omit them.
Safest interpretation: when a record is stored without a value, use `0` or `false` at the database/model layer; when a response is emitted, keep the schema contract consistent and use the field value as defined by the contract.

### 3.5 `date_from` and `date_to` validation
The list endpoint says inclusive boundaries, but does not say what to do when query params are partially missing or reversed.
Safest interpretation: if `date_from` and `date_to` are both present and `date_from > date_to`, return 422. If only one is present, filter from that bound only.

### 3.6 Competitors’ leaderboard row cutoff
The leaderboard says `limit` applies after ranking, and that rows can exceed the limit if they share a rank at the boundary.
Safest interpretation: compute rank first using competition ranking, then keep every employee whose rank is `<= limit` and sort by `total_late_minutes` descending, then `emp_code` ascending.

### 3.7 Department headcount and months
The rule says headcount includes employees who joined before the end of the period, but a department whose headcount is 0 is omitted from the summary.
Safest interpretation: compute active employees by department for the given month boundary, then discard departments with zero headcount.

### 3.8 Weekend and no-record trend rows
The trend endpoint requires exactly one row per calendar day, including weekends and empty days, but it also says attendance rate is only defined for working days with headcount > 0.
Safest interpretation: still emit the row for each day but return `attendance_rate = null` on weekends or non-working days; for working days with headcount 0, return `null`; for working days with headcount > 0 and no records, return `0.0`.

### 3.9 exact semantics of `present_count`
The trend endpoint says `present_count = presence-status records that day (half day = 0.5), any weekday or weekend.`
Safest interpretation: count the presence status records for that date, but use 0.5 if the record has `half_day = true`; otherwise count 1 for each presence record.

### 3.10 History values and API boundary conversions
The contract says history `from`/`to` values are BSON dates in MongoDB and epoch millisecond integers in the API, but this is easy to forget because the values are nested objects.
Safest interpretation: when serializing or deserializing the history array, translate nested timestamps to/from epoch milliseconds exactly at the API boundary and leave the stored MongoDB document as BSON datetimes.

## 4. Collections, fields, and indexes implied by the data model

### Collection: `employees`
MongoDB stores one document per employee.

Fields:
- `_id`: MongoDB ObjectId, internal only
- `emp_code`: string, unique business identifier, required for all APIs
- `name`: string
- `email`: string
- `department`: string, case-sensitive
- `shift_start`: string `HH:MM` in IST
- `shift_end`: string `HH:MM` in IST; if `shift_end <= shift_start`, it indicates an overnight shift
- `joined_on`: string `YYYY-MM-DD`
- `created_at`: BSON date, server-set at employee creation

Example shape:
```json
{
  "emp_code": "EMP0001",
  "name": "Asha Rao",
  "email": "asha@example.com",
  "department": "Engineering",
  "shift_start": "09:30",
  "shift_end": "18:30",
  "joined_on": "2026-01-05",
  "created_at": { "$date": "2026-01-05T04:30:00Z" }
}
```

Required indexes:
- unique index on `emp_code`
- index on `department`
- index on `joined_on`
- optional compound index on `(department, emp_code)` for filtering and sorted lookups

### Collection: `attendance_logs`
MongoDB stores one document per `(emp_code, date)` pair.

Fields:
- `_id`: MongoDB ObjectId, internal only
- `emp_code`: string, points to `employees.emp_code`
- `date`: string `YYYY-MM-DD`, the attendance day in IST
- `status`: one of `PRESENT`, `WFH`, `ON_DUTY`, `ABSENT`, `LEAVE`
- `punch_in`: BSON date or null
- `punch_out`: BSON date or null
- `work_hours`: double or null; computed by the API
- `late_minutes`: integer; computed by the API, missing means 0
- `overtime_minutes`: integer; computed by the API, missing means 0
- `half_day`: boolean; computed by the API, missing means false
- `history`: array of correction entries, empty list when none

Example status semantics:
- `PRESENT`, `WFH`, `ON_DUTY` require punch-in and count as present
- `ABSENT`, `LEAVE` have both punch times null and do not count as present

Required indexes:
- unique compound index on `(emp_code, date)`
- index on `(date, emp_code)` for date-range and sorting queries
- index on `status` for analytics filters
- optional index on `late_minutes` for leaderboard optimization

### Why these indexes are required
- The assignment explicitly says the app must create indexes at startup and idempotently.
- The duplicate create path (`emp_code` in employees; `(emp_code, date)` in attendance logs) must be protected by the database layer and not only by application logic.
- Analytics and list endpoints are strongly filtered or sorted by department, date, and employee.

## 5. Suggested build order

1. Configure the app and startup behavior
   - Load environment variables, create the MongoDB client, and initialize the database.
   - Create required indexes idempotently on startup.
   - Add the health endpoint first to verify the engine is reachable.

2. Implement employee creation and listing
   - `POST /employees`
   - `GET /employees`
   - Validate schema and regex constraints.
   - Enforce unique `emp_code` with a DB unique index plus a 409 error path.
   - Return exact response shapes and pagination semantics.

3. Implement the attendance primitives next
   - `POST /attendance/punch-in`
   - `POST /attendance/punch-out`
   - Validate time windows, overnight logic, and all derived fields.
   - Ensure database-level uniqueness on `(emp_code, date)`.

4. Implement the regularization and list flows
   - `GET /attendance`
   - `PATCH /attendance/{emp_code}/{date}`
   - Recompute derived fields after corrections and append a single history item.
   - Ensure no accidental overwrite of an existing correction history array.

5. Build the analytics pipelines last
   - Monthly employee summary
   - Department summary
   - Late leaderboard
   - Department trend
   - Validate that they read stored derived values and use aggregation rather than Python-side recomputation.

6. Hardening pass
   - Confirm 404, 409, 422 behavior.
   - Verify sort order, page skipping, `total` semantics, and competition ranking.
   - Confirm the MongoDB explain endpoints would use indexes and not full collection scans.

## 6. Subtle spec quotes worth preserving while implementing

> "Employees are identified by `emp_code`. It is a customer-generated business code assigned by the customer's HR team... It is unique, never changes, and is meant for humans. It is not MongoDB's `_id`. `_id` is internal: it never appears in a request or a response."

> "Derived fields are read-only. `work_hours`, `late_minutes`, `overtime_minutes` and `half_day` are computed by the API from the punch times, the employee's shift and rules R2-R5. They never appear in a request body. If a client sends one, ignore it."

> "For analytics, read the stored derived values; do not recompute them from the punch times."

> "A record that has never been corrected has `history: []`."

> "Exactly one row per calendar day from `from` to `to` inclusive - weekends and days with no records at all included (use a gap-filling stage such as `$densify`; do not loop in Python)."

> "The grader checks that the plan contains `IXSCAN` and no `COLLSCAN`, on a ~100k-record dataset."

## 7. Implementation notes for the code path

The code should follow these rules exactly:
- Use FastAPI request validation for all input and query param constraints.
- Convert MongoDB BSON datetimes to epoch milliseconds at the API boundary, not before storage.
- Convert `YYYY-MM-DD` strings to exact date strings and preserve them in responses.
- Enforce uniqueness in MongoDB with unique indexes, not just in Python.
- Use `page = 1` as the first page and `skip = (page - 1) * page_size`.
- Apply filtering before pagination and compute `total` from the filtered result set.
- Sort by `emp_code` ascending for employee list queries.
- Use the database indexes for filter-supporting query shapes; do not perform collection-wide scanning in Python.
