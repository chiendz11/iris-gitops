from __future__ import annotations

import argparse
import random
import time

import requests


SAMPLES = (
    {
        "sepal_length": 5.1,
        "sepal_width": 3.5,
        "petal_length": 1.4,
        "petal_width": 0.2,
    },
    {
        "sepal_length": 6.0,
        "sepal_width": 2.9,
        "petal_length": 4.5,
        "petal_width": 1.5,
    },
    {
        "sepal_length": 6.7,
        "sepal_width": 3.1,
        "petal_length": 5.6,
        "petal_width": 2.4,
    },
)
SPECIES = {"setosa", "versicolor", "virginica"}


def generate_traffic(url: str, request_count: int, interval: float = 0.05) -> None:
    if request_count < 1:
        raise ValueError("request_count must be positive")
    if interval < 0:
        raise ValueError("interval cannot be negative")
    for _ in range(request_count):
        response = requests.post(
            url,
            json={"instances": [random.choice(SAMPLES)]},  # nosec B311: load generation
            timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
        predictions = payload.get("predictions")
        if (
            not isinstance(predictions, list)
            or not predictions
            or any(prediction not in SPECIES for prediction in predictions)
        ):
            raise RuntimeError("Inference response does not match the prediction contract")
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate production canary traffic")
    parser.add_argument("--url", required=True)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--interval", type=float, default=0.05)
    args = parser.parse_args()
    generate_traffic(args.url, args.requests, args.interval)


if __name__ == "__main__":
    main()
