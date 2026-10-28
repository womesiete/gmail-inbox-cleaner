# Offline validation and measurements — 2026-10-28

The 28-item reliability plan is implemented in separate item commits. Verification uses synthetic messages, fixture transports, and the built-in offline AI. No real mailbox or private AI provider was used. These historical measurements used a Linux implementation host; its interpreter version is omitted, and they do not validate the currently supported runtime. The checked application code tree is `b77b171f42ef50e0c3efdc83d8b8d08759c1a7c8`; historical baseline commit is `1ff85fd1eab4b2e7d35f3067c95d4615d3e51734`.

## Final checks

- 451 tests passed; application line coverage was 82.98%, above the 75% gate.
- Expanded Ruff B/BLE/S/PERF, all 16 non-Qt mypy modules, compilation, and dependency consistency passed.
- The pinned dependency audit reported no known vulnerabilities.
- Source and wheel builds passed. Windows frozen-executable building and its offline smoke test are also required by hosted CI; consult the final workflow run for that platform result.
- Refactor characterization passed before and after extraction. GUI classes/functions retained identical ASTs and all comments; job comments/docstrings and decision reasons were retained.

## Before and after

Each workload used a fresh data directory. The pipeline, SQL paging model, and full job are separate measurements. These are single observations on a shared execution host, with other validation activity possible. They are not controlled production latency or provider-billing measurements.

| Workload / measurement | Before | After |
| --- | ---: | ---: |
| 3,000-message headless invocation | 27.5609 s | 21.6380 s |
| 50,000-message full job, processing only | 412.0230 s | 259.3289 s |
| Full-job synthetic mailbox creation | 2.9267 s | 2.5864 s |
| 50,000-row SQL model: seed | 7.8980 s | 8.2352 s |
| SQL model: first page | 0.004736 s | 0.005475 s |
| SQL model: last page | 0.037970 s | 0.052349 s |
| SQL model: maximum cached pages | 8 | 8 |

The 3,000-message run completed 7,844 stage items. The 50,000-message job used AI batches of 10 and retrieval batches of 50, completing 130,783 stage items and reserving 4,178 offline AI calls. Both ended at Completed / Review with zero failed, pending, or retry items. Stage-item counts include metadata, body, and classification work and are not distinct-message counts. SQL paging timings increased slightly in this run; the full pipeline improved. No broad live-Gmail speed claim follows from these observations.

The transaction regression measured 42 commits per eight-message metadata/body batch before batching, then at most three for metadata and four for bodies, including state/heartbeat writes. Mailbox calls are asserted to occur outside SQLite write transactions. Network facade counts are recorded separately in [network performance](network-performance.md).

Reproduce with the project virtual environment and a fresh private base for each command:

```bash
python scripts/ci_tests.py
python -m ruff check gmail_clean tests scripts
python -m mypy
python -m build
python -m gmail_clean --demo --headless --count 3000 --data-dir <fresh-base>
python -m gmail_clean --demo --benchmark 50000 --data-dir <another-fresh-base>
python -m gmail_clean --demo --job-benchmark 50000 --data-dir <third-fresh-base>
```

The baseline full-job harness used the same service pipeline and batch settings before the benchmark CLI existed. Structured measurements, without mailbox text or local paths, are in [the JSON record](performance-2026-10-28.json). CI generates a separate [validation record](validation.md) from JUnit/coverage summaries. Live OAuth, consent policy, private provider behavior, and target-machine credentials still require configured-environment testing.
