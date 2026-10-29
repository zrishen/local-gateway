"""In-process request count used to avoid maintenance during AI streams."""
from contextlib import contextmanager
import threading

_lock = threading.Lock()
_active = 0


@contextmanager
def ai_request():
    global _active
    with _lock:
        _active += 1
    try:
        yield
    finally:
        with _lock:
            _active -= 1


def active_requests():
    with _lock:
        return _active
