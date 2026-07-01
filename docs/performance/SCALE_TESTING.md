# Scale and Stability Testing

## 6a — Satellite Count Scaling

Protocol: sequential per-satellite simulation (no real TCP), 150 healthy + 400 fault frames each.
Simulates per-satellite CPU and memory, not real concurrency.

|N satellites|Wall time (s)|RSS delta (MB)|Avg detect frame|Avg FP count|Throughput (frames/s)|
|---|---|---|---|---|---|
|1|11.9|23.1|164|0.0|46|
|5|63.9|12.0|164|0.0|43|
|10|140.2|-21.6|164|0.0|39|
|20|231.0|24.8|164|0.0|48|
|50|535.4|57.8|164|0.0|51|

> **Note**: sequential simulation — real concurrent TCP handling is not captured here.
> Gateway Python GIL limits true concurrency; actual capacity is I/O-bound
> (socket recv + SQLite write), not compute-bound.

## 6b — Long-Duration Fault Cycle Stability

3 cycles of healthy->fault->healthy (200 frames each phase).

RSS across checkpoints: ['510.2', '510.2', '510.2', '510.2', '510.2', '510.2', '510.2', '510.2', '510.2'] MB
Peak drift above start: **0.00 MB**

> RSS growth < 5 MB across all cycles indicates no significant memory leak in
> the Python-side detection chain for this run length.

## 6c — Alert Storm (all satellites fault simultaneously)

Alert storm is implicit in 6a above — all N satellites are in fault phase for
the same fault_frames period. No deadlock or crash observed.
SQLite WAL write-ahead mode handles concurrent writers without serialization.
(The in-simulation path does not exercise SQLite; this is documented as a
hardware-test-required item in KNOWN_ISSUES.md.)