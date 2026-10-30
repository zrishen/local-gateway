from pathlib import Path
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
import gateway_activity as activity


class GatewayActivityTests(unittest.TestCase):
    def setUp(self):
        activity._active = 0
        activity._started.clear()

    def test_active_count_and_oldest_age_are_snapshotted_together(self):
        release = threading.Event()

        def hold():
            with activity.ai_request():
                release.wait(2)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(self._wait_for(lambda: activity.active_requests() == 1))
        time.sleep(.05)
        active, oldest = activity.request_stats()
        self.assertEqual(active, 1)
        self.assertGreater(oldest, 0)
        with activity.ai_request():
            active, nested = activity.request_stats()
            self.assertEqual(active, 2)
            self.assertGreaterEqual(nested, oldest)
        release.set()
        thread.join(2)
        self.assertEqual(activity.active_requests(), 0)
        self.assertEqual(activity.request_stats(), (0, 0.0))

    def test_parallel_requests_do_not_lose_count(self):
        barrier = threading.Barrier(6)
        errors = []

        def hold():
            try:
                with activity.ai_request():
                    barrier.wait(2)
                    time.sleep(.02)
            except Exception as error:  # pragma: no cover - diagnostic
                errors.append(error)

        threads = [threading.Thread(target=hold) for _ in range(6)]
        for thread in threads:
            thread.start()
        self.assertTrue(self._wait_for(lambda: activity.active_requests() == 6))
        for thread in threads:
            thread.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(activity.active_requests(), 0)

    @staticmethod
    def _wait_for(predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(.01)
        return False


if __name__ == '__main__':
    unittest.main()
