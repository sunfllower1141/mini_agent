"""Tests for tools/_json_rpc_shared.py."""

from __future__ import annotations

import subprocess
import tempfile
import unittest

from tools._json_rpc_shared import _drain, drain_stderr, is_subprocess_connected


class TestDrainStderr(unittest.TestCase):
    """Tests for drain_stderr()."""

    def test_returns_none_when_process_is_none(self) -> None:
        """drain_stderr returns None when given None process."""
        thread = drain_stderr(None)
        self.assertIsNone(thread)

    def test_returns_none_when_stderr_is_none(self) -> None:
        """drain_stderr returns None when process has no stderr pipe."""
        proc = subprocess.Popen(
            ["true"],
            stdout=subprocess.PIPE,
            stderr=None,
        )
        try:
            self.assertIsNone(proc.stderr)
            thread = drain_stderr(proc)
            self.assertIsNone(thread)
        finally:
            proc.wait()

    def test_returns_thread_and_drains_stderr(self) -> None:
        """drain_stderr starts a daemon thread that drains stderr."""
        proc = subprocess.Popen(
            ["python3", "-c", "import sys; sys.stderr.write('hello stderr\\n'); sys.stderr.flush()"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        thread = drain_stderr(proc)
        self.assertIsNotNone(thread)
        self.assertTrue(thread.daemon)
        self.assertEqual(thread.name, "jsonrpc-stderr")
        # Let the thread drain
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        proc.wait()

    def test_custom_thread_name(self) -> None:
        """drain_stderr respects the thread_name parameter."""
        proc = subprocess.Popen(
            ["python3", "-c", "import sys; sys.stderr.write('x\\n')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        thread = drain_stderr(proc, thread_name="custom-drain")
        self.assertIsNotNone(thread)
        self.assertEqual(thread.name, "custom-drain")
        thread.join(timeout=5)
        proc.wait()

    def test_drain_prevents_deadlock(self) -> None:
        """Writing enough stderr to fill the pipe buffer does not deadlock."""
        # Write ~128KB to stderr — more than the default pipe buffer
        proc = subprocess.Popen(
            ["python3", "-c", "import sys; sys.stderr.write('x' * 200000); sys.stderr.flush()"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        thread = drain_stderr(proc)
        self.assertIsNotNone(thread)
        # The process should complete without deadlocking
        proc.wait(timeout=10)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())


class TestDrain(unittest.TestCase):
    """Tests for the internal _drain() helper."""

    def test_drains_all_lines(self) -> None:
        """_drain reads all lines from the stream."""
        with tempfile.TemporaryFile(mode="w+") as f:
            f.write("line1\nline2\nline3\n")
            f.flush()
            f.seek(0)
            _drain(f)
            # After draining, the stream should be at EOF
            self.assertEqual(f.read(), "")

    def test_handles_closed_stream(self) -> None:
        """_drain handles a closed file gracefully."""
        with tempfile.TemporaryFile(mode="w+") as f:
            f.write("data\n")
            f.flush()
            f.close()
            # Should not raise
            _drain(f)

    def test_handles_non_iterable(self) -> None:
        """_drain handles streams that fail on iteration (OSError/ValueError)."""
        # A mock that raises on iteration
        class BadStream:
            def __init__(self) -> None:
                self.called = False

            def __iter__(self) -> None:
                self.called = True
                raise OSError("broken pipe")

        stream = BadStream()
        _drain(stream)  # should not raise
        self.assertTrue(stream.called)


class TestIsSubprocessConnected(unittest.TestCase):
    """Tests for is_subprocess_connected()."""

    def test_returns_false_when_none(self) -> None:
        """is_subprocess_connected returns False for None."""
        self.assertFalse(is_subprocess_connected(None))

    def test_returns_false_when_terminated(self) -> None:
        """is_subprocess_connected returns False for a terminated process."""
        proc = subprocess.Popen(
            ["python3", "-c", "exit(0)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proc.wait()
        self.assertFalse(is_subprocess_connected(proc))

    def test_returns_true_when_alive(self) -> None:
        """is_subprocess_connected returns True for a running process."""
        proc = subprocess.Popen(
            ["sleep", "5"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.assertTrue(is_subprocess_connected(proc))
        finally:
            proc.kill()
            proc.wait()

    def test_returns_false_after_process_completes(self) -> None:
        """is_subprocess_connected transitions to False after completion."""
        proc = subprocess.Popen(
            ["python3", "-c", "exit(0)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertTrue(is_subprocess_connected(proc))
        proc.wait()
        self.assertFalse(is_subprocess_connected(proc))


if __name__ == "__main__":
    unittest.main()
