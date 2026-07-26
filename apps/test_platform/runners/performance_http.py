"""Small built-in HTTP load driver for common smoke/performance checks."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from math import ceil
from time import monotonic, perf_counter
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx


class HttpPerformanceDriver:
    """Run bounded GET load against ``payload.inputs.url``.

    This is intentionally a smoke/load driver, not a distributed load engine.
    Larger workloads keep using the existing injected driver contract.
    """

    def __init__(self, *, request_timeout_seconds: float = 10.0) -> None:
        if not 0 < request_timeout_seconds <= 60:
            raise ValueError("request_timeout_seconds 必须在 0-60 秒之间")
        self.request_timeout_seconds = float(request_timeout_seconds)

    def _worker(self, url: str, deadline: float, headers: Mapping[str, str]) -> tuple[list[float], int, int]:
        latencies: list[float] = []
        failures = 0
        attempts = 0
        with httpx.Client(timeout=self.request_timeout_seconds, follow_redirects=False) as client:
            while attempts == 0 or monotonic() < deadline:
                attempts += 1
                started = perf_counter()
                try:
                    response = client.get(url, headers=dict(headers))
                    if not 200 <= response.status_code < 400:
                        failures += 1
                    response.read()
                except Exception:
                    failures += 1
                latencies.append((perf_counter() - started) * 1000)
        return latencies, failures, attempts

    def run(self, payload: Mapping[str, Any], context) -> dict[str, Any]:
        inputs = payload.get("inputs")
        if not isinstance(inputs, Mapping):
            raise ValueError("HTTP performance driver requires inputs")
        url = str(inputs.get("url") or "").strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("HTTP performance driver requires an absolute credential-free URL")
        headers = inputs.get("headers")
        if headers is None:
            headers = {}
        if not isinstance(headers, Mapping):
            raise ValueError("HTTP performance driver headers must be an object")
        all_latencies: list[float] = []
        failures = 0
        attempts = 0
        for stage in payload.get("stages") or []:
            deadline = monotonic() + float(stage["duration_seconds"])
            workers = int(stage["virtual_users"])
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(self._worker, url, deadline, headers)
                    for _ in range(workers)
                ]
                for future in futures:
                    latencies, worker_failures, worker_attempts = future.result()
                    all_latencies.extend(latencies)
                    failures += worker_failures
                    attempts += worker_attempts
        if not all_latencies:
            raise ValueError("HTTP performance driver made no requests")
        values = sorted(all_latencies)

        def percentile(value: float) -> float:
            return values[max(0, ceil(len(values) * value) - 1)]

        return {
            "metrics": {
                "latency_ms": {
                    "p50": percentile(0.50),
                    "p95": percentile(0.95),
                    "p99": percentile(0.99),
                    "unit": "ms",
                },
                "error_rate": failures / max(1, attempts),
                "request_count": attempts,
            }
        }


__all__ = ["HttpPerformanceDriver"]
