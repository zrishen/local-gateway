import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from stream_metrics import StreamMetrics


class StreamMetricsTests(unittest.TestCase):
    def test_first_text_is_distinct_from_first_event_and_handles_fragmentation(self):
        metrics = StreamMetrics(started=0)
        with patch('stream_metrics.time.monotonic', side_effect=[1, 2, 3, 4, 5]):
            metrics.headers('text/event-stream')
            metrics.feed(b'data: {"type":"response.created"}\n\n')
            text = ('data: ' + json.dumps({'type': 'response.output_text.delta', 'delta': '中文'}) + '\n\n').encode()
            metrics.feed(text[:25])
            metrics.feed(text[25:])
            metrics.feed(b'data: {"type":"response.completed"}\n\n')
        result = metrics.snapshot()
        self.assertEqual(result['headers_seconds'], 1)
        self.assertEqual(result['first_byte_seconds'], 2)
        self.assertEqual(result['first_text_seconds'], 4)
        self.assertEqual(result['stream_terminal'], 'response.completed')
        self.assertNotIn('中文', json.dumps(result))

    def test_large_image_events_are_skipped_without_hiding_next_terminal(self):
        metrics = StreamMetrics()
        metrics.headers('text/event-stream')
        metrics.feed(b'data: {"image":"' + b'a' * 200000)
        self.assertLessEqual(len(metrics.buffer), 131072)
        metrics.feed(b'"}\n\ndata: {"type":"response.completed"}\n\n')
        self.assertEqual(metrics.terminal_event, 'response.completed')

    def test_compressed_stream_is_not_misparsed_as_plaintext(self):
        metrics = StreamMetrics()
        metrics.headers('text/event-stream', 'gzip')
        self.assertFalse(metrics.sse)

    def test_large_completed_event_is_recognized_at_frame_boundary(self):
        metrics = StreamMetrics()
        metrics.headers('text/event-stream')
        metrics.feed(b'event: response.completed\ndata: {"output":"' + b'a' * 200000)
        self.assertIsNone(metrics.terminal_event)
        metrics.feed(b'"}\n\n')
        self.assertEqual(metrics.terminal_event, 'response.completed')
