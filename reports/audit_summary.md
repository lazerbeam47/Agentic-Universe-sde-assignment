# Controller audit evidence

The following values were queried from the controller's own SQLite stores after each scenario finished. The databases were kept in `/tmp` to avoid committing generated call data.

```sql
SELECT action, COUNT(*) FROM audit GROUP BY action ORDER BY action;
SELECT ts, workflow_id, details FROM audit
WHERE action='population_kpis' ORDER BY id DESC LIMIT 1;
```

| Scenario | Cursor | Unique events | Checkpoints | KPI snapshots | Starts / finishes |
|---|---:|---:|---:|---:|---|
| public_01 | 104 | 297,003 | 191 | 573 | 2 / 1 |
| public_02 | 133 | 324,308 | 240 | 720 | 1 / 1 |

The public_01 workflow audit records two controller starts because the first process was interrupted and resumed. Both scenarios have one final `controller_finished` record. Public_02's compact archive retained 142,011,267 raw event bytes in 5,977,368 compressed chunk bytes; the 10,412,032-byte SQLite database measured about 32.1 MB per million events, with 4,434,664 bytes in indexed-table and index overhead. Event chunks were compressed while preserving raw payload content.

Example final public_02 `ks_cancel` snapshot at `2027-01-14T00:00:00+05:30`:

```json
{"dials":113009,"connected":25229,"engaged":12347,"pickup_rate":0.2232477059,"engagement_rate":0.4893971224}
```
