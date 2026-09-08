"""Real PTY checks for large, incremental terminal input."""
import importlib.util
import io
import os
import pty
import termios
import threading
import time
import unittest


class TerminalInputTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("terminal_input"), "nonblocking terminal input is missing")
        from terminal_input import TTYInput
        self.master, slave = pty.openpty()
        self.stream = os.fdopen(slave, "r", encoding="utf-8")
        self.before = termios.tcgetattr(slave)
        self.reader = TTYInput(self.stream, io.StringIO())
        self.reader.__enter__()
        self.addCleanup(os.close, self.master)
        self.addCleanup(self.stream.close)
        self.addCleanup(self.reader.__exit__, None, None, None)

    def queue_paste(self, data):
        def write():
            offset = 0
            while offset < len(data):
                offset += os.write(self.master, data[offset:])

        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        writer.join(timeout=3)
        self.assertFalse(writer.is_alive(), "PTY reader did not drain the kernel buffer")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.reader.incoming.mutex:
                received = sum(len(chunk) for chunk in self.reader.incoming.queue)
            if received == len(data):
                return
            time.sleep(0.001)
        self.fail("Paste did not reach the reader queue")

    def test_terminal_ask_drains_queued_long_paste_without_per_chunk_waits(self):
        from auto_model import Terminal
        expected = "x" * 128000 + "\nčeština"
        self.queue_paste(b"\x1b[200~" + expected.encode() + b"\x1b[201~\n")
        ui = Terminal(self.stream, io.StringIO(), self.reader.output, interactive=True)
        ui.reader = self.reader
        started = time.monotonic()

        def idle():
            self.assertLess(time.monotonic() - started, 1, "Already queued paste waits between chunks")

        ui.idle = idle
        self.assertEqual(ui.ask(""), expected)
        self.assertLess(time.monotonic() - started, 1)

    def test_large_unfinished_paste_yields_to_idle_before_draining_everything(self):
        from auto_model import Terminal
        expected = "x" * (1024 * 1024)
        self.queue_paste(b"\x1b[200~" + expected.encode())
        ui = Terminal(self.stream, io.StringIO(), self.reader.output, interactive=True)
        ui.reader = self.reader
        progress = []
        started = time.monotonic()

        def idle():
            self.assertLess(time.monotonic() - started, 3, "Paste monopolized the input loop")
            if not progress:
                self.assertGreater(len(self.reader.chars), 0)
                self.assertLess(len(self.reader.chars), len(expected), "Input drain starved idle service")
                os.write(self.master, b"\x1b[201~\n")
            progress.append(len(self.reader.chars))

        ui.idle = idle
        self.assertEqual(ui.ask(""), expected)
        self.assertTrue(progress, "Large input must yield to background service")

    def test_long_paste_is_not_limited_by_canonical_line_size(self):
        data = b"\x1b[200~" + b"x" * 3000 + b"\nsecond line\x1b[201~"
        os.write(self.master, data)
        self.assertIsNone(self.reader.read_line())
        os.write(self.master, b"\n")
        self.assertEqual(self.reader.read_line(wait=True), "x" * 3000 + "\nsecond line")

    def test_partial_paste_does_not_block_and_queues_separate_tasks(self):
        os.write(self.master, b"\x1b[200~one\ntwo")
        self.assertIsNone(self.reader.read_line())
        os.write(self.master, b"\x1b[201~\nnext\n")
        self.assertEqual(self.reader.read_line(wait=True), "one\ntwo")
        self.assertEqual(self.reader.read_line(), "next")

    def test_unicode_backspace_and_cancellation(self):
        os.write(self.master, "češtinax".encode() + b"\x7f\n")
        self.assertEqual(self.reader.read_line(wait=True), "čeština")
        with self.assertRaises(EOFError):
            self.reader.read_line(wait=True, canceled=lambda: True)

    def test_original_terminal_settings_are_restored(self):
        self.reader.__exit__(None, None, None)
        restored = termios.tcgetattr(self.stream.fileno())
        # macOS marks pending input for reprocessing when ICANON is restored.
        # PENDIN is kernel state (termios(4)), not an editable mode we changed.
        pending = getattr(termios, "PENDIN", 0)
        restored[3] &= ~pending
        self.before[3] &= ~pending
        self.assertEqual(restored, self.before)


if __name__ == "__main__":
    unittest.main()
