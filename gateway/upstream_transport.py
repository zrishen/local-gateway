"""Bounded AI upstream retries before a response is exposed to the client.

Diagnostics deliberately exclude URLs, addresses, headers and request bodies.
"""
import http.client
import json
import logging
from pathlib import Path
import socket
import ssl
import threading
import time
import uuid

EVENT_PATH = Path(__file__).resolve().parents[1] / 'logs/ai-upstream.jsonl'
CONNECT_TIMEOUT = 12
READ_TIMEOUT = 600
MAX_ATTEMPTS = 2
RETRY_STATUSES = {502, 503, 504}
_log_lock = threading.Lock()


def record_event(event):
    try:
        with _log_lock:
            EVENT_PATH.parent.mkdir(parents=True, exist_ok=True)
            with EVENT_PATH.open('a', encoding='utf-8') as log:
                log.write(json.dumps({'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), **event}) + '\n')
    except OSError:
        logging.warning('AI upstream diagnostic log unavailable')


class UpstreamUnavailable(Exception):
    def __init__(self, request_id, phase, cause, attempts):
        super().__init__('AI upstream connection failed')
        self.request_id = request_id
        self.phase = phase
        self.cause = cause
        self.attempts = attempts

    def payload(self):
        return {'error': {'type': 'upstream_connection_error',
                          'message': 'AI upstream unavailable; see logs/ai-upstream.jsonl',
                          'request_id': self.request_id, 'phase': self.phase,
                          'cause': self.cause, 'attempts': self.attempts}}


def request_upstream(factory, method, target, body, headers):
    """Return (connection, response, request_id, attempts), transferring ownership.

    Transport failures retry only before sending the request. Ambiguous failures
    while sending/waiting are never replayed. Explicit 502/503/504 responses may
    retry once for inference POSTs and reads, before forwarding any headers/body.
    """
    request_id = uuid.uuid4().hex
    can_retry = method in {'GET', 'HEAD', 'POST'}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        started = time.monotonic()
        conn = None
        phase = 'connect'
        try:
            conn = factory()
            conn.timeout = CONNECT_TIMEOUT
            conn.connect()
            conn.sock.settimeout(READ_TIMEOUT)
            phase = 'send'
            conn.request(method, target, body=body, headers=headers)
            phase = 'headers'
            response = conn.getresponse()
        except (OSError, http.client.HTTPException, ValueError) as error:
            if conn:
                conn.close()
            retry = (can_retry and phase == 'connect' and attempt < MAX_ATTEMPTS
                     and not isinstance(error, (ssl.SSLCertVerificationError, ValueError)))
            cause = type(error).__name__
            record_event({'request_id': request_id, 'attempt': attempt,
                          'phase': phase, 'cause': cause,
                          'winerror': getattr(error, 'winerror', None),
                          'retry': retry, 'seconds': round(time.monotonic() - started, 3)})
            if not retry:
                raise UpstreamUnavailable(request_id, phase, cause, attempt) from None
        else:
            retry = can_retry and response.status in RETRY_STATUSES and attempt < MAX_ATTEMPTS
            record_event({'request_id': request_id, 'attempt': attempt,
                          'phase': phase, 'status': response.status, 'retry': retry,
                          'seconds': round(time.monotonic() - started, 3)})
            if not retry:
                return conn, response, request_id, attempt
            # Do not wait for or expose a failed response's potentially slow body.
            response.close()
            conn.close()
        time.sleep(.5)


def error_payload(status, request_id, attempts):
    return {'error': {'type': 'upstream_http_error',
                      'message': 'AI upstream returned HTTP %d; see logs/ai-upstream.jsonl' % status,
                      'request_id': request_id, 'attempts': attempts}}
