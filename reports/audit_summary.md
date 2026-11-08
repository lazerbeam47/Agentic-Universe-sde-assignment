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
| public_02 | 130 | 324,282 | 240 | 720 | 1 / 1 |

The public_01 workflow audit records two controller starts because the first process was interrupted and resumed. Both scenarios have one final `controller_finished` record. The audit stores per-call event payloads with config version/cohort/variant and hourly workflow-level KPI snapshots; pickup and engagement are kept as separate values.

Example final public_02 `ks_cancel` snapshot at `2027-01-14T00:00:00+05:30`:

```json
{"dials":113009,"connected":25229,"engaged":12347,"pickup_rate":0.2232477059,"engagement_rate":0.4893971224}
```
