"""Observe stream timing without modifying bytes or logging model content."""
import json
import time


class StreamMetrics:
    def __init__(self, started=None):
        self.started = time.monotonic() if started is None else started
        self.headers_seconds = None
        self.connect_seconds = None
        self.upload_seconds = None
        self.first_byte_seconds = None
        self.first_text_seconds = None
        self.max_gap_seconds = 0.0
        self.previous = None
        self.sse = False
        self.terminal_event = None
        self.error = None
        self.eof = False
        self.buffer = b''
        self.skipping_line = False
        self.frame_event = None

    def headers(self, content_type, encoding=''):
        self.headers_seconds = time.monotonic() - self.started
        self.sse = ('text/event-stream' in (content_type or '')
                    and (encoding or '').lower() in ('', 'identity'))

    def feed(self, chunk):
        now = time.monotonic()
        if self.first_byte_seconds is None:
            self.first_byte_seconds = now - self.started
        if self.previous is not None:
            self.max_gap_seconds = max(self.max_gap_seconds, now - self.previous)
        self.previous = now
        if not self.sse:
            return
        if (self.first_text_seconds is not None and self.frame_event not in
                ('response.completed', 'response.failed', 'response.incomplete')):
            # Once text starts, ordinary token deltas need no JSON/line parsing.
            # Retain only an unfinished line suffix for split terminal markers.
            scan = self.buffer[-256:] + chunk
            if not any(marker in scan for marker in (
                    b'response.completed', b'response.failed', b'response.incomplete')):
                self.buffer = scan.rsplit(b'\n', 1)[-1][-256:]
                self.skipping_line = False
                return
        # Image events can contain megabytes of base64. Limit observer memory;
        # the original chunk is still forwarded in full by the caller.
        for index, part in enumerate(chunk.split(b'\n')):
            if index:
                if not self.skipping_line:
                    self._line(self.buffer, now)
                self.buffer = b''
                self.skipping_line = False
            if len(self.buffer) + len(part) > 131072:
                self.buffer = b''
                self.skipping_line = True
            elif not self.skipping_line:
                self.buffer += part

    def _line(self, line, now):
        if line.startswith(b'event:'):
            self.frame_event = line[6:].strip().decode('ascii', errors='ignore')
            return
        if not line.strip():
            if self.frame_event in ('response.completed', 'response.failed', 'response.incomplete'):
                self.terminal_event = self.frame_event
            self.frame_event = None
            return
        if not line.startswith(b'data:'):
            return
        if not any(marker in line for marker in (
                b'response.completed', b'response.failed', b'response.incomplete')):
            if self.first_text_seconds is not None or b'response.output_text.delta' not in line:
                return
        try:
            event = json.loads(line[5:])
        except (ValueError, UnicodeError):
            return
        if not isinstance(event, dict):
            return
        kind = event.get('type')
        if kind == 'response.output_text.delta' and event.get('delta') and self.first_text_seconds is None:
            self.first_text_seconds = now - self.started
        if kind in ('response.completed', 'response.failed', 'response.incomplete'):
            self.terminal_event = kind

    def snapshot(self):
        return {
            'connect_seconds': self._round(self.connect_seconds),
            'upload_seconds': self._round(self.upload_seconds),
            'headers_seconds': self._round(self.headers_seconds),
            'first_byte_seconds': self._round(self.first_byte_seconds),
            'first_text_seconds': self._round(self.first_text_seconds),
            # Inter-chunk gaps include model computation, not only network time.
            'max_chunk_gap_seconds': self._round(self.max_gap_seconds),
            'stream_eof': self.eof,
            'stream_terminal': self.terminal_event,
            'stream_error': self.error,
        }

    @staticmethod
    def _round(value):
        return round(value, 3) if value is not None else None
