"""Incremental POSIX terminal input, without canonical-mode paste limits."""
import codecs
from collections import deque
import os
import queue
import select
import termios
import threading
import tty
import unicodedata


class TTYInput:
    def __init__(self, stream, output):
        self.fd = stream.fileno()
        self.output = output
        self.settings = None
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.chars, self.lines = [], deque()
        self.escape = ""
        self.pasting = False
        self.eof = False
        self.incoming = queue.Queue()
        self.stopped = threading.Event()
        self.thread = None

    def __enter__(self):
        self.settings = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd, termios.TCSANOW)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        if self.settings is not None:
            self.stopped.set()
            self.thread.join(timeout=0.5)
            termios.tcsetattr(self.fd, termios.TCSANOW, self.settings)
            self.settings = None

    def _read(self):
        # Drain the small macOS tty buffer even while classification is running.
        while not self.stopped.is_set():
            if select.select([self.fd], [], [], 0.05)[0]:
                try:
                    data = os.read(self.fd, 16384)
                except OSError:
                    data = b""
                self.incoming.put(data)
                if not data:
                    return

    def read_line(self, *, wait=False, canceled=lambda: False):
        # ponytail: 256 KiB per poll; use a time budget if parsing gets expensive.
        remaining = 256 * 1024
        while True:
            if canceled():
                raise EOFError
            if self.lines:
                return self.lines.popleft()
            if self.eof:
                raise EOFError
            try:
                data = self.incoming.get(timeout=0.15 if wait else 0)
            except queue.Empty:
                if wait:
                    continue
                return None
            if data is not None:
                remaining -= len(data)
                if not data:
                    self.eof = True
                    continue
                echo = []
                for char in self.decoder.decode(data):
                    if self.escape:
                        self.escape += char
                        if self.escape == "\x1b[200~":
                            self.pasting, self.escape = True, ""
                        elif self.escape == "\x1b[201~":
                            self.pasting, self.escape = False, ""
                        elif len(self.escape) > 32 or (len(self.escape) > 2 and "@" <= char <= "~"):
                            self.escape = ""
                        continue
                    if char == "\x1b":
                        self.escape = char
                    elif char == "\x04" and not self.pasting:
                        if self.chars:
                            self.lines.append("".join(self.chars))
                            self.chars.clear()
                        else:
                            self.eof = True
                    elif char in ("\x7f", "\b") and not self.pasting:
                        if self.chars:
                            removed = self.chars.pop()
                            width = 0 if unicodedata.combining(removed) else 2 if unicodedata.east_asian_width(removed) in "WF" else 1
                            echo.append("\b \b" * width)
                    elif char in ("\r", "\n") and not self.pasting:
                        self.lines.append("".join(self.chars))
                        self.chars.clear()
                        echo.append("\n")
                    elif char.isprintable() or char in ("\n", "\t"):
                        self.chars.append(char)
                        echo.append(char)
                if echo:
                    self.output.write("".join(echo))
                    self.output.flush()
                if self.lines:
                    continue
            if not wait and remaining <= 0:
                return None
