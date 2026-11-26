"""Mini-Prometheus: a runnable, dependency-light Prometheus subset.

The package mirrors the architecture of Prometheus (discovery -> scrape ->
TSDB -> rules -> HTTP API) closely enough that the design lessons transfer,
while keeping the storage engine and query engine deliberately small and
auditable.  Only the Python standard library and PyYAML are required.

The on-disk formats are documented in chapters 14-16 and 33-36.  They are *not*
byte-compatible with upstream Prometheus; the API surface and PromQL
semantics are where compatibility is pursued.
"""

__version__ = "1.0.0"
USER_AGENT = "MiniPrometheus/1.0.0"
