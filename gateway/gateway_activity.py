"""In-process request tracking used to avoid maintenance during AI streams."""
from contextlib import contextmanager
import threading
import time

_lock = threading.Lock()
_active = 0
_started = {}
_next_token = 0


@contextmanager
def ai_request():
    global _active, _next_token
    with _lock:
        _active += 1
        _next_token += 1
        token = _next_token
        _started[token] = time.monotonic()
    try:
        yield
    finally:
        with _lock:
            _active = max(0, _active - 1)
            _started.pop(token, None)


def active_requests():
    with _lock:
        return _active


def request_stats():
    """Return ``(active_count, oldest_age_seconds)`` in one atomic snapshot."""
    now = time.monotonic()
    with _lock:
        active = _active
        oldest = max((now - started for started in _started.values()), default=0.0)
    return active, (oldest if active else 0.0)
