from __future__ import annotations

from automation.model_release import smoke


def test_smoke_uses_the_v1_object_contract(monkeypatch) -> None:
    payloads: list[dict] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"predictions": ["setosa"]}

    def fake_post(_url: str, *, json: dict, timeout: int) -> Response:
        assert timeout == 5
        payloads.append(json)
        return Response()

    monkeypatch.setattr(smoke.requests, "post", fake_post)
    monkeypatch.setattr(smoke.time, "sleep", lambda _seconds: None)
    smoke.generate_traffic("http://inference/v1/models/iris:predict", 3)

    assert len(payloads) == 3
    assert set(payloads[0]["instances"][0]) == {
        "sepal_length",
        "sepal_width",
        "petal_length",
        "petal_width",
    }
