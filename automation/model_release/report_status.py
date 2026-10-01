"""Exit-handler diagnostics. Reporting does not mutate serving or MLflow."""
from __future__ import annotations

import json
import os


def report(status: str, name: str, failures: str) -> dict:
    return {
        "workflow": name,
        "status": status,
        "requires_investigation": status != "Succeeded",
        "failures": failures,
        "message": (
            "Inspect GitOps main, live serving and champion before recovery. "
            "Workflow failure does NOT imply traffic rollback. "
            "See docs/RECOVERY.md."
        ),
    }


if __name__ == "__main__":
    print(json.dumps(report(
        os.environ["LIFECYCLE_STATUS"], os.environ["LIFECYCLE_NAME"],
        os.environ.get("LIFECYCLE_FAILURES", ""),
    )))
