"""Tiny, cached 'am I online?' check (no data sent anywhere)."""
from __future__ import annotations

import socket
import time

_last = {"t": 0.0, "v": False}


def is_online(timeout: float = 1.5, ttl: float = 30.0) -> bool:
    now = time.time()
    if now - _last["t"] < ttl:
        return _last["v"]
    ok = False
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            with socket.create_connection((host, 443), timeout=timeout):
                ok = True
                break
        except OSError:
            continue
    _last.update(t=now, v=ok)
    return ok
