# Offline quota validation

The demo now records the same request-unit charges as the live adapter in a
`QuotaLimiter.consumed` counter. Its simulated quota clock advances without real
network waits. These are modeled units from synthetic traffic, not a live Gmail
throughput benchmark. Job summaries expose `quota_usage` by stage, including retries.

The deterministic `test_unchanged_pipeline_consumes_fewer_modeled_quota_units`
workload processes 20 old inbox messages in separate threads, retrieval batches of
five and AI batches of one, using the existing recording AI fake. Each message
requires its body and classification. Both runs finish all 60 work stages with
zero failures and 20 assessments.

| Stage | Cache disabled: units/message | History-covered cache: units/message |
| --- | ---: | ---: |
| Discovery | 0.4 | 0.4 |
| Metadata and thread evidence | 60 | 60.4 |
| Body preflight and body retrieval | 120 | 60.4 |
| Classification preflight | 60 | 2 |
| Total | 240.4 | 123.2 |

The total falls from 4,808 to 2,464 units (48.75%) for this unchanged workload.
Changes, expired history, cache expiry, contextual body retrieval and execution
preflights add requests; this ratio is not a general performance guarantee.

`metadata_cache_ttl_seconds` defaults to 60 and accepts 0–300; zero disables reuse.
The cache lives only within one job runner and is discarded on job completion.
Before reuse, every history page from its recorded cursor is checked. Changed
messages and their threads are evicted. Unknown thread membership clears thread
evidence; expired history forces a full read. History transport errors stop reuse.
Complete history coverage renews unchanged evidence, including across long stage
transitions; the TTL bounds reuse without that proof. The demo uses the same
simulated clock for its quota limiter and cache, and a regression advances it ten
minutes between stages. An expired history-page cursor also forces a fresh read.
No execution or undo preflight uses this cache.
