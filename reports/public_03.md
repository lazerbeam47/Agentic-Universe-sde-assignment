# public_03_load — blocked after storage review

The earlier scenario server completed its seven-day historical bootstrap in **65.6 seconds**. Its generated temporary run directory reached **2.3 GiB**. At controller startup the host reported only **117 MiB free** on the shared data volume; writing the durable SQLite history failed with `sqlite3.OperationalError: database or disk is full` before the first simulated advance.

The controller process used a measured maximum resident set of **556,138,496 bytes** and exited after **4.64 seconds** (including failed history ingestion). These figures describe the failed bootstrap attempt only; they are **not** ingest throughput, decision lag, or a completed load benchmark. The generated run directory was removed after the failure, restoring approximately 2.4 GiB. After the compact-storage changes, a new public_03 run was attempted, but automatic approval review rejected launching another multi-gigabyte simulator run while the shared volume had only about 2.7 GiB available. No second public_03 benchmark result is claimed.

Compact chunk storage was measured on public_02: 324,308 events, 142,011,267 raw bytes, 5,977,368 compressed chunk bytes, and a 10,412,032-byte SQLite database (about 32.1 MB per million events, including normalized indexes and audit). That smaller run does not establish public_03 peak memory, bootstrap throughput, or full-run disk use. A public_03 run remains outstanding and requires a reviewed environment with safe disk headroom.

## Controlled synthetic storage measurement

A bounded synthetic test streamed 6,000 small call events through `save_chunk_lines` without building an input list. It measured 916,893 raw bytes, 17,878 compressed chunk bytes, and 155,648 SQLite page bytes: approximately 25.94 MB of database pages per million of these synthetic events. Python `tracemalloc` recorded a 324,541-byte peak during that ingestion. This is a small single-chunk synthetic measurement; it excludes SQLite's native allocations and is not an RSS measurement or a simulator benchmark. The synthetic payload omitted the wider set of dimensions present in the real event stream, so its per-million storage rate is not directly comparable to public_02's 32.1 MB.

The ingestion path consumes the line iterator incrementally and commits one source chunk in a transaction. The largest measured public_02 source chunk was 81,936,109 raw bytes; that bound still depends on the simulator's chunking. The full public_03 run's batch and disk growth remain unmeasured here.
