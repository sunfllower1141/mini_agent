#!/usr/bin/env python3
"""test_interject.py — tests for the thread-safe user interjection queue."""

import threading
import unittest

import interject


class TestInterject(unittest.TestCase):
    """Tests for push_interjection, poll_interjections, has_interjections."""

    def setUp(self):
        """Clear module-level interjection state before each test."""
        with interject._LOCK:
            interject._INTERJECTIONS.clear()

    # ------------------------------------------------------------------
    # Single push / poll
    # ------------------------------------------------------------------

    def test_single_push_poll(self):
        interject.push_interjection("hello")
        self.assertTrue(interject.has_interjections())
        result = interject.poll_interjections()
        self.assertEqual(result, ["hello"])
        self.assertFalse(interject.has_interjections())

    def test_push_empty_string(self):
        interject.push_interjection("")
        result = interject.poll_interjections()
        self.assertEqual(result, [""])

    # ------------------------------------------------------------------
    # Multiple pushes
    # ------------------------------------------------------------------

    def test_multiple_pushes(self):
        interject.push_interjection("a")
        interject.push_interjection("b")
        interject.push_interjection("c")
        self.assertTrue(interject.has_interjections())
        result = interject.poll_interjections()
        self.assertEqual(result, ["a", "b", "c"])
        self.assertFalse(interject.has_interjections())

    def test_poll_clears_queue(self):
        interject.push_interjection("x")
        interject.push_interjection("y")
        interject.poll_interjections()
        # second poll should return empty
        result = interject.poll_interjections()
        self.assertEqual(result, [])

    # ------------------------------------------------------------------
    # Empty / no-interjection cases
    # ------------------------------------------------------------------

    def test_empty_poll_returns_empty_list(self):
        self.assertEqual(interject.poll_interjections(), [])
        self.assertFalse(interject.has_interjections())

    def test_has_interjections_false_initially(self):
        self.assertFalse(interject.has_interjections())

    def test_poll_twice_on_empty_queue(self):
        self.assertEqual(interject.poll_interjections(), [])
        self.assertEqual(interject.poll_interjections(), [])

    # ------------------------------------------------------------------
    # Thread-safe interleaving
    # ------------------------------------------------------------------

    def test_thread_safe_interleaving(self):
        errors: list[Exception] = []

        def worker(n: int) -> None:
            try:
                for i in range(10):
                    interject.push_interjection(f"t{n}-{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"thread errors: {errors}")
        items = interject.poll_interjections()
        self.assertEqual(len(items), 50, f"expected 50 items, got {len(items)}")
        self.assertFalse(interject.has_interjections())

    def test_concurrent_push_and_poll(self):
        """Push from one thread while polling from another."""
        push_done = threading.Event()
        errors: list[Exception] = []

        def pusher():
            try:
                for i in range(100):
                    interject.push_interjection(str(i))
            except Exception as exc:
                errors.append(exc)
            finally:
                push_done.set()

        def poller():
            seen = 0
            while not push_done.is_set() or interject.has_interjections():
                batch = interject.poll_interjections()
                seen += len(batch)
            return seen

        t_push = threading.Thread(target=pusher)
        t_poll = threading.Thread(target=poller)

        t_push.start()
        t_poll.start()
        t_push.join()
        t_poll.join()

        self.assertEqual(len(errors), 0)

    def test_has_interjections_during_concurrent_push(self):
        """has_interjections should eventually return True when pushing concurrently."""

        def slow_push():
            interject.push_interjection("msg")

        t = threading.Thread(target=slow_push)
        t.start()
        t.join()

        self.assertTrue(interject.has_interjections())
        interject.poll_interjections()
