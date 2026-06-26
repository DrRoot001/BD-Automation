"""Prometheus Custom Business Metrics."""

from prometheus_client import Counter, Histogram, Gauge

# Applications
applications_queued = Counter(
    "bd_applications_queued_total", 
    "Total applications queued for processing", 
    ["platform"]
)

applications_submitted = Counter(
    "bd_applications_submitted_total", 
    "Total applications successfully submitted", 
    ["platform"]
)

applications_failed = Counter(
    "bd_applications_failed_total", 
    "Total applications that failed", 
    ["reason"]
)

# LLM 
llm_scoring_duration = Histogram(
    "bd_llm_scoring_seconds", 
    "Time spent in LLM scoring per job",
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0]
)

# WebSocket
active_ws_connections = Gauge(
    "bd_ws_connections_active", 
    "Number of currently active WebSocket connections"
)
