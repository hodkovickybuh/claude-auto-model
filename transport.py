"""Claude Code's persistent JSON-lines control transport (stdlib only)."""
import json
import os
import queue
import signal
import subprocess
import threading
import time
import uuid


class Engine:
    def __init__(self, command, *, timeout=60, on_event=None, on_request=None, on_idle=None, env=None):
        self.timeout = timeout
        self.on_event = on_event or (lambda event: None)
        self.on_request = on_request
        self.on_idle = on_idle
        self.events = queue.Queue()
        self.responses = {}
        self.pending_results = []
        self.canceled_requests = set()
        self.handled_requests = set()
        self.active_user_id = None
        self.session_id = ""
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, env=env,
            start_new_session=True,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("expected JSON object")
                    if event.get("type") == "control_cancel_request":
                        self.canceled_requests.add(event.get("request_id"))
                    self.events.put(event)
                except (ValueError, TypeError):
                    self.events.put(RuntimeError("Claude emitted invalid stream-json"))
                    return
        finally:
            self.events.put(RuntimeError("Claude control stream closed"))

    def send(self, event):
        if self.process.poll() is not None:
            raise RuntimeError(f"Claude exited with code {self.process.returncode}")
        try:
            self.process.stdin.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RuntimeError("Claude control stream closed") from exc

    def _next(self, timeout=None):
        try:
            event = self.events.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError("Claude did not acknowledge the control request") from exc
        if isinstance(event, Exception):
            raise event
        if event.get("session_id"):
            self.session_id = event["session_id"]
        return event

    def _dispatch(self, event):
        if event.get("session_id"):
            self.session_id = event["session_id"]
        kind = event.get("type")
        if kind == "control_response":
            response = event.get("response", {})
            self.responses[response.get("request_id")] = response
        elif kind == "control_request":
            request = event.get("request", {})
            if event.get("request_id") in self.handled_requests:
                return
            if event.get("request_id") in self.canceled_requests:
                self.handled_requests.add(event["request_id"])
                self.canceled_requests.discard(event["request_id"])
                return
            try:
                if self.on_request is None:
                    if request.get("subtype") == "can_use_tool":
                        answer = {"behavior": "deny", "message": "No permission handler attached"}
                    else:
                        raise RuntimeError("Unsupported Claude control request")
                else:
                    answer = self.on_request({**request, "_is_canceled":
                                              lambda: event["request_id"] in self.canceled_requests})
                response = {"subtype": "success", "request_id": event["request_id"], "response": answer}
            except Exception as exc:
                response = {"subtype": "error", "request_id": event["request_id"], "error": str(exc)}
            if event["request_id"] not in self.canceled_requests:
                self.send({"type": "control_response", "response": response})
            self.canceled_requests.discard(event["request_id"])
            self.handled_requests.add(event["request_id"])
        elif kind == "result":
            inputs = event.get("user_message_uuids") or [event.get("user_message_uuid")]
            background = bool(event.get("origin")) and self.active_user_id not in inputs
            owned = False
            if self.active_user_id and not background:
                if self.active_user_id in inputs or (event.get("is_error") and not any(inputs)):
                    self.pending_results.append(event)
                    owned = True
                elif not any(inputs):
                    raise RuntimeError("Claude result lacks user correlation; update Claude Code (tested on 2.1.263)")
            self.on_event(event if owned else {**event, "_router_background": True})
        else:
            self.on_event(event)

    def poll(self):
        """Service background tool requests while the user composes a prompt."""
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            if isinstance(event, Exception):
                raise event
            self._dispatch(event)

    def request(self, subtype, **fields):
        request_id = str(uuid.uuid4())
        self.send({"type": "control_request", "request_id": request_id,
                   "request": {"subtype": subtype, **fields}})
        deadline = time.monotonic() + self.timeout
        while request_id not in self.responses:
            self._dispatch(self._next(max(0, deadline - time.monotonic())))
        response = self.responses.pop(request_id)
        if response.get("subtype") != "success":
            raise RuntimeError(response.get("error", "Claude rejected control request"))
        for pending in response.get("pending_permission_requests", []):
            self._dispatch(pending)
        return response.get("response") or {}

    def turn(self, prompt):
        if self.pending_results:
            raise RuntimeError("Previous turn has unconsumed results")
        self.active_user_id = str(uuid.uuid4())
        self.send({"type": "user", "session_id": self.session_id, "uuid": self.active_user_id,
                   "message": {"role": "user", "content": prompt},
                   "parent_tool_use_id": None})
        return self.finish_turn()

    def finish_turn(self):
        # Tasks may run for hours. Only control acknowledgments have a deadline.
        while not self.pending_results:
            if self.on_idle:
                self.on_idle()
            try:
                event = self._next(0.15 if self.on_idle else None)
            except TimeoutError:
                continue
            self._dispatch(event)
        result = self.pending_results.pop(0)
        self.active_user_id = None
        return result

    def close(self):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
            except ProcessLookupError:
                pass
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()
        self.reader.join(timeout=1)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
