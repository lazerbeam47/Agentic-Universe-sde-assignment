# public_03_load — attempted run

The scenario server completed its seven-day historical bootstrap in **65.6 seconds**. Its generated temporary run directory reached **2.3 GiB**. At controller startup the host reported only **117 MiB free** on the shared data volume; writing the durable SQLite history failed with `sqlite3.OperationalError: database or disk is full` before the first simulated advance.

The controller process used a measured maximum resident set of **556,138,496 bytes** and exited after **4.64 seconds** (including failed history ingestion). These figures describe the failed bootstrap attempt only; they are **not** ingest throughput, decision lag, or a completed load benchmark. The generated run directory was removed after the failure, restoring approximately 2.4 GiB. The controller did not finish public_03, so this is an explicit storage/environment limitation, not a passing load result.

To make this benchmark viable, run with a volume that has several gigabytes free for simulator state plus controller event history, or store the append-only audit/events on a provisioned database. A compact or partitioned event representation with an explicit retention policy is needed before asserting 10× scale.
