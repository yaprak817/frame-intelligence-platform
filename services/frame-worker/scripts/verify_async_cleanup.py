"""Verify and consume the task-scoped async harness cleanup receipt."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from run_async_integration import REQUIRED_CLEANUP_STEPS


def main() -> int:
    value = os.environ.get("ASYNC_CLEANUP_RECEIPT")
    if not value:
        print("cleanup_verification_failed=RECEIPT_CONFIG", file=sys.stderr)
        return 1
    receipt = Path(value)
    try:
        payload = json.loads(receipt.read_text(encoding="ascii"))
        clean = (
            receipt.is_file()
            and payload.get("status") == "clean"
            and payload.get("verified") == list(REQUIRED_CLEANUP_STEPS)
        )
    except Exception:
        clean = False
    if not clean:
        print("cleanup_verification_failed=RECEIPT_MISSING", file=sys.stderr)
        return 1
    try:
        receipt.unlink()
    except Exception:
        print("cleanup_verification_failed=RECEIPT_CLEANUP", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
