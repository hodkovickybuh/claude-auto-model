"""Offline process-boundary checks. Run: python3 -m unittest -v."""
import importlib.util
import json
import os
import signal
import sys
import threading
import unittest
from unittest.mock import patch


# A tiny executable peer, not a mocked Engine: catches ordering, framing and EOF.
PEER = r'''
import json, os, sys, time
model, effort, turns = "sonnet", "high", 0
for line in sys.stdin:
    event = json.loads(line)
    if event["type"] == "control_request":
        req, rid = event["request"], event["request_id"]
        result = {}
        if req["subtype"] == "set_model":
            if req["model"] == "bad":
                print(json.dumps({"type":"control_response", "response":{
                    "subtype":"error", "request_id":rid, "error":"unavailable"}}), flush=True)
                continue
            model = req["model"]
        elif req["subtype"] == "apply_flag_settings":
            effort = req["settings"]["effortLevel"]
        elif req["subtype"] == "die":
            sys.exit(7)
        elif req["subtype"] == "hang":
            continue
        response = {"subtype":"success", "request_id":rid, "response":result}
        if req["subtype"] == "replay":
            permission = {"type":"control_request", "request_id":"permission-one", "request":{
                "subtype":"can_use_tool", "tool_name":"Bash", "input":{"command":"echo example"}}}
            response["pending_permission_requests"] = [permission, permission]
        print(json.dumps({"type":"control_response", "response":response}), flush=True)
    elif event["type"] == "user":
        if event["message"]["content"] == "slow":
            time.sleep(0.25)
        turns += 1
        if event["message"]["content"] == "with background":
            print(json.dumps({"type":"result", "subtype":"success", "result":"BACKGROUND",
                "origin":{"kind":"task-notification"}, "user_message_uuid":"not-the-active-input"}), flush=True)
            print(json.dumps({"type":"result", "subtype":"success", "result":"SYNTHETIC",
                "origin":{"kind":"auto-continuation"}}), flush=True)
        print(json.dumps({"type":"result", "subtype":"success", "session_id":"same-session",
            "user_message_uuid":event.get("uuid"),
            "result":json.dumps([model,effort,turns,os.getpid(),event["message"]["content"]])}), flush=True)
'''


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if importlib.util.find_spec("transport") is not None:
            from transport import Engine
            cls.Engine = Engine

    def setUp(self):
        self.assertTrue(hasattr(self, "Engine"), "persistent Engine is not implemented")
        self.engine = self.Engine([sys.executable, "-u", "-c", PEER], timeout=1)
        self.addCleanup(self.engine.close)

    def test_model_and_effort_switch_before_next_turn_same_process(self):
        self.engine.request("set_model", model="fable")
        self.engine.request("apply_flag_settings", settings={"effortLevel": "max"})
        first = self.engine.turn("hard task")
        self.engine.request("set_model", model="haiku")
        self.engine.request("apply_flag_settings", settings={"effortLevel": "low"})
        second = self.engine.turn("easy task")
        a, b = json.loads(first["result"]), json.loads(second["result"])
        self.assertEqual(a[:3], ["fable", "max", 1])
        self.assertEqual(b[:3], ["haiku", "low", 2])
        self.assertEqual(a[3], b[3])
        self.assertEqual(first["session_id"], second["session_id"])

    def test_rejected_switch_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.engine.request("set_model", model="bad")

    def test_closed_child_fails_promptly(self):
        with self.assertRaisesRegex(RuntimeError, "closed|exit|ended"):
            self.engine.request("die")

    def test_control_timeout_is_bounded(self):
        with self.assertRaises(TimeoutError):
            self.engine.request("hang")

    def test_prompt_is_data_not_shell_code(self):
        prompt = 'line one\n$(exit 42) `exit 42` "quotes" čeština'
        self.assertEqual(json.loads(self.engine.turn(prompt)["result"])[4], prompt)

    def test_running_turn_services_local_status_without_new_message(self):
        ticks = []
        self.engine.on_idle = lambda: ticks.append(True)
        result = self.engine.turn("slow")
        self.assertTrue(ticks)
        self.assertEqual(json.loads(result["result"])[2], 1)

    def test_background_result_never_finishes_active_turn(self):
        result = self.engine.turn("with background")
        self.assertEqual(json.loads(result["result"])[4], "with background")
        self.assertEqual(json.loads(self.engine.turn("next")["result"])[2], 2)

    def test_pending_permissions_replay_once(self):
        seen = []
        self.engine.on_request = lambda req: seen.append(req) or {"behavior":"deny", "message":"test"}
        self.engine.request("replay")
        self.assertEqual(len(seen), 1)

    def test_canceled_permission_never_reappears_on_replay(self):
        seen = []
        self.engine.on_request = lambda req: seen.append(req) or {"behavior":"deny", "message":"test"}
        self.engine.canceled_requests.add("permission-one")
        self.engine.request("replay")
        self.assertEqual(seen, [])

    @unittest.skipUnless(hasattr(os, "fork"), "POSIX process groups required")
    def test_close_kills_stubborn_descendant_even_if_leader_exits_first(self):
        peer = r'''
import json, os, signal, sys, time
ready_read, ready_write = os.pipe()
if os.fork() == 0:
    os.close(ready_read)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.write(ready_write, b"ready")
    os.close(ready_write)
    time.sleep(20)
    os._exit(0)
os.close(ready_write)
os.read(ready_read, 5)
os.close(ready_read)
print(json.dumps({"type":"system", "pgid":os.getpgrp()}), flush=True)
if sys.argv[1] == "exited":
    os._exit(0)
time.sleep(20)
'''
        for leader in ("running", "exited", "sigkill-denied"):
            with self.subTest(leader=leader):
                engine = self.Engine([sys.executable, "-u", "-c", peer, leader])
                errors = []
                real_killpg = os.killpg

                def kill_group(pgid, sig):
                    if leader == "sigkill-denied" and pgid == engine.process.pid and sig == signal.SIGKILL:
                        raise PermissionError("fixture: live descendant cannot be signaled")
                    return real_killpg(pgid, sig)

                def close():
                    try:
                        with patch("transport.os.killpg", side_effect=kill_group):
                            engine.close()
                    except BaseException as exc:
                        errors.append(exc)

                closer = threading.Thread(target=close, daemon=True)
                try:
                    ready = engine._next(2)
                    self.assertEqual(ready["pgid"], engine.process.pid)
                    self.assertNotEqual(ready["pgid"], os.getpgrp())
                    if leader == "exited":
                        engine.process.wait(timeout=2)
                    closer.start()
                    closer.join(2)
                    self.assertFalse(closer.is_alive(), "close blocked on descendant's stdout")
                    if leader == "sigkill-denied":
                        self.assertEqual(len(errors), 1)
                        self.assertIsInstance(errors[0], PermissionError)
                        self.assertTrue(engine.reader.is_alive())
                    else:
                        self.assertEqual(errors, [])
                        self.assertFalse(engine.reader.is_alive(), "descendant still holds stdout open")
                finally:
                    # Only this Engine's newly created process group is targeted.
                    try:
                        real_killpg(engine.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    engine.process.wait(timeout=2)
                    if closer.ident is None:
                        closer.start()
                    closer.join(2)
                    self.assertFalse(closer.is_alive(), "fixture cleanup did not finish")
                    engine.reader.join(2)
                    engine.process.stdin.close()
                    engine.process.stdout.close()

    def test_repeated_close_does_not_signal_a_reusable_process_group_id(self):
        self.engine.close()
        with patch("transport.os.killpg", side_effect=AssertionError("closed group signaled again")):
            self.engine.close()

    @unittest.skipUnless(hasattr(os, "fork"), "POSIX process groups required")
    def test_close_handles_zombie_only_group_after_leader_is_reaped(self):
        peer = r'''
import json, os, time
leader = os.getpid()
ready_read, ready_write = os.pipe()
reaper = os.fork()
if reaper == 0:
    # Keep a zombie in the Engine group after its leader has been reaped.
    os.setpgid(0, 0)
    worker = os.fork()
    if worker == 0:
        os.setpgid(0, leader)
        os.write(ready_write, b"ready")
        os._exit(0)
    os.close(1)
    time.sleep(20)
    os.waitpid(worker, 0)
    os._exit(0)
print(json.dumps({"type":"system", "reaper":reaper}), flush=True)
os.close(ready_write)
os.read(ready_read, 5)
os._exit(0)
'''
        engine = self.Engine([sys.executable, "-u", "-c", peer])
        reaper = None
        try:
            reaper = engine._next(2)["reaper"]
            engine.process.wait(timeout=2)
            engine.reader.join(2)
            self.assertFalse(engine.reader.is_alive())
            try:
                engine.close()
            except PermissionError as exc:
                self.fail(f"zombie-only group prevented close: {exc}")
            self.assertTrue(engine.process.stdin.closed)
            self.assertTrue(engine.process.stdout.closed)
        finally:
            if reaper is not None:
                try:
                    os.kill(reaper, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                os.killpg(engine.process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            engine.process.wait(timeout=2)
            engine.reader.join(2)
            engine.process.stdin.close()
            engine.process.stdout.close()


if __name__ == "__main__":
    unittest.main()
