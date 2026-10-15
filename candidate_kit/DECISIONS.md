# Decisions

1. I created unique employee `emp_code` and attendance `(emp_code, date)` indexes, plus supporting indexes for department/code and department/join-date lookups, date-sorted and status/date attendance queries, and latest employee punch-in. They support filters, pagination, and race-safe writes. More indexes would add write cost without serving these queries.
2. Concurrent punch-ins may both read the employee, but use the same unique natural key. MongoDB accepts one insert; duplicate-key becomes 409. The index, not a check-then-insert, guarantees one winner.
3. Ties share standard competition rank. I include every row with rank <= `limit`, even if that exceeds the limit, and order tied rows by `emp_code`.
4. The summary starts from eligible employees and preserves them through lookup/unwind when logs are absent. Distinct employee codes count headcount; future joiners are excluded.
5. At 100x, I would measure bottlenecks, then consider precomputed daily aggregates and archiving. I would retain natural-key uniqueness and reconcile aggregates with source records.
