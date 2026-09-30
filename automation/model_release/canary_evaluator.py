from __future__ import annotations

import argparse
import json
import math
import re
import time
from pathlib import Path

import requests


SAFE_LABEL = re.compile(r"^[A-Za-z0-9._-]+$")


def query(prometheus_url: str, expression: str) -> float:
    response = requests.get(
        f"{prometheus_url.rstrip('/')}/api/v1/query",
        params={"query": expression},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise ValueError("Prometheus did not return a successful query result")
    result = payload["data"]["result"]
    # Missing telemetry is not a zero-latency, zero-error observation.
    return float(result[0]["value"][1]) if result else math.nan


def candidate_selector(service: str, model_version: str) -> str:
    if not SAFE_LABEL.fullmatch(service):
        raise ValueError("service contains unsupported label characters")
    if not re.fullmatch(r"[0-9]+", model_version):
        raise ValueError("model_version must be numeric")
    return f'service="{service}",model_version="{model_version}"'


def evaluate(
    prometheus_url: str,
    service: str,
    model_version: str,
    max_p95: float,
    max_error_rate: float,
    min_requests: int = 20,
) -> dict[str, float | bool | str]:
    selector = candidate_selector(service, model_version)
    rps = query(
        prometheus_url,
        f"sum(rate(iris_prediction_requests_total{{{selector}}}[5m]))",
    )
    errors = query(
        prometheus_url,
        "sum(rate(iris_prediction_requests_total"
        f'{{{selector},status!="success"}}[5m])) or vector(0)',
    )
    p95 = query(
        prometheus_url,
        "histogram_quantile(0.95, sum by (le) "
        f"(rate(iris_prediction_latency_seconds_bucket{{{selector}}}[5m])))",
    )
    samples = query(
        prometheus_url,
        f"sum(increase(iris_prediction_latency_seconds_count{{{selector}}}[5m]))",
    )
    if min_requests < 1 or max_p95 <= 0 or not 0 <= max_error_rate <= 1:
        raise ValueError("Invalid canary thresholds")
    complete = all(math.isfinite(value) and value >= 0 for value in (rps, errors, p95, samples))
    sufficient = complete and rps > 0 and samples >= min_requests and errors <= rps
    error_rate = errors / rps if rps > 0 and math.isfinite(rps) and math.isfinite(errors) else None
    passed = bool(sufficient and p95 <= max_p95 and error_rate <= max_error_rate)
    decision = ("pass" if passed else "reject") if sufficient else "inconclusive"
    return {
        "passed": passed,
        "decision": decision,
        "service": service,
        "model_version": model_version,
        "rps": rps if math.isfinite(rps) else None,
        "p95_seconds": p95 if math.isfinite(p95) else None,
        "samples": samples if math.isfinite(samples) else None,
        "error_rate": error_rate,
    }


def wait_for_candidate_metrics(
    prometheus_url: str,
    service: str,
    model_version: str,
    max_p95: float,
    max_error_rate: float,
    timeout_seconds: int,
    poll_seconds: int,
    min_requests: int = 20,
) -> dict[str, float | bool | str]:
    """Only a complete observation can reject; telemetry errors stay inconclusive."""
    if timeout_seconds < 0 or poll_seconds <= 0:
        raise ValueError("Invalid observation polling settings")
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            result = evaluate(
                prometheus_url, service, model_version, max_p95, max_error_rate,
                min_requests,
            )
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            result = {
                "passed": False, "decision": "inconclusive",
                "reason": f"Telemetry unavailable: {type(error).__name__}",
            }
        if result["decision"] != "inconclusive" or time.monotonic() >= deadline:
            return result
        time.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate only the candidate model metrics")
    parser.add_argument("--prometheus-url", required=True)
    parser.add_argument("--service", default="iris-classifier")
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--max-p95", type=float, default=0.5)
    parser.add_argument("--max-error-rate", type=float, default=0.01)
    parser.add_argument("--observation-timeout", type=int, default=120)
    parser.add_argument("--poll-seconds", type=int, default=10)
    parser.add_argument("--min-requests", type=int, default=20)
    parser.add_argument("--decision-output", type=Path, default=Path("/tmp/canary-decision.txt"))
    parser.add_argument("--output", type=Path, default=Path("/tmp/canary-result.json"))
    parser.add_argument(
        "--passed-output", type=Path, default=Path("/tmp/canary-passed.txt")
    )
    parser.add_argument("--fail-on-reject", action="store_true")
    args = parser.parse_args()
    result = wait_for_candidate_metrics(
        args.prometheus_url,
        args.service,
        args.model_version,
        args.max_p95,
        args.max_error_rate,
        args.observation_timeout,
        args.poll_seconds,
        args.min_requests,
    )
    args.output.write_text(json.dumps(result), encoding="utf-8")
    args.passed_output.write_text(str(result["passed"]).lower(), encoding="utf-8")
    args.decision_output.write_text(result["decision"], encoding="utf-8")
    print(json.dumps(result))
    if result["decision"] == "inconclusive":
        raise SystemExit(2)
    if args.fail_on_reject and not result["passed"]:
        raise SystemExit("Candidate canary SLO gate failed")


if __name__ == "__main__":
    main()
