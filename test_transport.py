"""Offline process-boundary checks. Run: python3 -m unittest -v."""
import importlib.util
import json
import sys
import unittest


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


if __name__ == "__main__":
    unittest.main()
