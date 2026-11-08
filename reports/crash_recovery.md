# Crash recovery evidence — public_01_dev

The controller was interrupted with Ctrl+C during a live public_01 run while it was handling events (the first process stopped inside population aggregation). The simulator process and its state were left running. The controller was then restarted with the same SQLite file:

```sh
python3 controller.py http://127.0.0.1:8700 --db /tmp/controller-public01.sqlite3
```

On restart, the API reported the existing world and workflow state. The durable event cursor was chunk 52; the controller requested history/events after that cursor, resumed `/sim/advance`, and completed the scenario. The final audit database showed:

| Check | Observed |
|---|---:|
| Controller starts | 2 |
| Controller finishes | 1 |
| Last durable chunk | 104 |
| Unique ingested events | 297,003 |
| Loop checkpoints | 191 |
| Duplicate event IDs | 0 (primary key plus `INSERT OR IGNORE`) |

The official public_01 grade completed and the run reached the scenario's finished state. The grader reported no changes applied and no governance violations. Temporary simulator and SQLite run data were stored under `/tmp`, not committed.
