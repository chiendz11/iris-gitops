import pytest

from automation.model_release import canary_evaluator


def test_candidate_metrics_are_filtered_by_model_version(monkeypatch: pytest.MonkeyPatch) -> None:
    expressions: list[str] = []

    def fake_query(_url: str, expression: str) -> float:
        expressions.append(expression)
        if "status!=" in expression:
            return 0.001
        if "histogram_quantile" in expression:
            return 0.2
        if "increase(" in expression:
            return 50.0
        return 1.0

    monkeypatch.setattr(canary_evaluator, "query", fake_query)
    result = canary_evaluator.evaluate(
        "http://prometheus",
        "iris-classifier",
        "12",
        max_p95=0.5,
        max_error_rate=0.01,
    )

    assert result["passed"] is True
    assert all('model_version="12"' in expression for expression in expressions)
    assert all('service="iris-classifier"' in expression for expression in expressions)


def test_candidate_without_traffic_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(canary_evaluator, "query", lambda *_args: 0.0)
    result = canary_evaluator.evaluate(
        "http://prometheus",
        "iris-classifier",
        "12",
        max_p95=0.5,
        max_error_rate=0.01,
    )
    assert result["passed"] is False
    assert result["decision"] == "inconclusive"


def test_waits_for_prometheus_to_scrape_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    results = iter([
        {"passed": False, "decision": "inconclusive", "rps": 0.0},
        {"passed": True, "decision": "pass", "rps": 1.0},
    ])
    sleeps: list[int] = []
    monkeypatch.setattr(canary_evaluator, "evaluate", lambda *_args: next(results))
    monkeypatch.setattr(canary_evaluator.time, "sleep", sleeps.append)
    times = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr(canary_evaluator.time, "monotonic", lambda: next(times))

    result = canary_evaluator.wait_for_candidate_metrics(
        "http://prometheus",
        "iris-classifier",
        "12",
        0.5,
        0.01,
        timeout_seconds=120,
        poll_seconds=10,
    )

    assert result["passed"] is True
    assert sleeps == [10]


@pytest.mark.parametrize("latency,errors,decision", [
    (0.6, 0.0, "reject"), (0.2, 0.02, "reject"),
    (float("nan"), 0.0, "inconclusive"), (0.2, 0.0, "pass"),
])
def test_complete_metrics_reject_but_missing_metrics_do_not(monkeypatch, latency, errors, decision):
    values = iter([1.0, errors, latency, 50.0])
    monkeypatch.setattr(canary_evaluator, "query", lambda *_: next(values))
    result = canary_evaluator.evaluate("http://prometheus", "iris-classifier", "12", 0.5, 0.01)
    assert result["decision"] == decision


def test_prometheus_failure_is_inconclusive(monkeypatch):
    def unavailable(*_):
        raise canary_evaluator.requests.ConnectionError("unavailable")
    monkeypatch.setattr(canary_evaluator, "evaluate", unavailable)
    result = canary_evaluator.wait_for_candidate_metrics(
        "http://prometheus", "iris-classifier", "12", 0.5, 0.01, 0, 10,
    )
    assert result["decision"] == "inconclusive"
    assert not result["passed"]
