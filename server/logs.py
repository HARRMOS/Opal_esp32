"""Journal minimal : une ligne horodatée par événement (lu via `docker logs`)."""

from __future__ import annotations

import time


def log(event: str, **fields: object) -> None:
    extra = " ".join(f"{k}={v!r}" for k, v in fields.items())
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {event} {extra}".rstrip(), flush=True)
