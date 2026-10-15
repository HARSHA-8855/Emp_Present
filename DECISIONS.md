# DECISIONS.md

1. **Indexes.** I created a unique index on `emp_code` in the employees collection and a unique compound index on `(emp_code, date)` in the attendance logs. That keeps duplicate employees out and ensures there is only one attendance record per person per day. I also keep `(department, emp_code)` and `(date, emp_code)` because those match the filters I use most often for employee listing and analytics. I did not keep a separate `status` index because it is not selective enough and it was not the index Mongo chose in explain().

2. **Punch-in race.** If two identical punch-in requests land at the same time, both of them pass the usual validation, but only one insert can succeed against the unique `(emp_code, date)` index. The second request gets a duplicate-key error, and the API turns that into a 409 response. That means the database itself handles the race cleanly without me relying on a fragile Python-only lock.

3. **Ties.** For the late-minute leaderboard, I sort by total late minutes descending and then by `emp_code` ascending. That makes the ranking deterministic when two people have the same total and keeps the cutoff fair and predictable instead of depending on insertion order.

4. **Headcount.** The department summary counts people from the employees collection, not just from attendance logs. So even if a person has no attendance entries in that month, they still count toward the department headcount and the attendance rate.

5. **One thing I would change** if this had to serve 100x the data. I would build a separate summary collection for monthly department and employee rollups and refresh it in the background. That would let the API read small, denormalized results instead of re-aggregating the full attendance set every time a dashboard request comes in.
