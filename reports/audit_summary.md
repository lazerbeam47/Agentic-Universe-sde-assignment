# Controller audit evidence

The following values were queried from the controller's own SQLite stores after each scenario finished. The databases were kept in `/tmp` to avoid committing generated call data.

```sql
SELECT action, COUNT(*) FROM audit GROUP BY action ORDER BY action;
SELECT ts, workflow_id, details FROM audit
WHERE action='population_kpis' ORDER BY id DESC LIMIT 1;
```

| Scenario | Cursor | Unique events | Checkpoints | KPI snapshots | Starts / finishes |
|---|---:|---:|---:|---:|---|
| public_01 clean final run | 109 | 297,023 | 191 | 573 | 1 / 1 |
| public_02 clean final run + finish-state reconciliation | 139 | 324,311 | 239 | 717 | 2 / 2 |

Public_01 completed in one controller process (8.58 seconds wall time). Public_02 completed its 10 simulated days in 10.07 seconds; a second controller start at the already-finished simulator state reconciled the final-tick experiment stop and one final event chunk without advancing time (0.019 seconds). Public_02's final compact archive retained 141,964,609 raw event bytes in 5,991,550 compressed chunk bytes; the 10,452,992-byte SQLite database measured about 32.2 MB per million events, with 4,461,442 bytes in indexed-table and index overhead. Event chunks were compressed while preserving raw payload content.

Example final public_02 `ks_cancel` snapshot at `2027-01-14T00:00:00+05:30`:

```json
{"dials":113009,"connected":25292,"engaged":12713,"pickup_rate":0.2238051837,"engagement_rate":0.5026490590}
```
