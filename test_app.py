import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class AppTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("auto_model"), "terminal controller is not implemented")
        import auto_model
        self.app = auto_model

    def test_flags_after_prompt_are_manual_locks(self):
        args = self.app.parse_args(["hard task", "--model", "opus", "--effort", "max"])
        self.assertEqual((args.prompt, args.model, args.effort), ("hard task", "opus", "max"))

    def test_permission_defaults_to_deny_without_person(self):
        ui = self.app.Terminal(io.StringIO(), io.StringIO(), io.StringIO())
        result = ui.permission({"tool_name": "Bash", "input": {"command": "publish"}})
        self.assertEqual(result["behavior"], "deny")

    def test_multiline_bracketed_paste_is_one_task(self):
        ui = self.app.Terminal(io.StringIO("\x1b[200~line one\nline two\x1b[201~\n"), io.StringIO(), io.StringIO())
        self.assertEqual(ui.ask(""), "line one\nline two")

    def test_permission_requires_explicit_yes(self):
        output = io.StringIO()
        ui = self.app.Terminal(io.StringIO("yes abc123\n"), output, output, interactive=True)
        with patch("auto_model.secrets.token_hex", return_value="abc123"):
            result = ui.permission({"tool_name": "Bash", "input": {"command": "echo example"}})
        self.assertEqual(result, {"behavior": "allow", "updatedInput": {"command": "echo example"}})
        self.assertIn("echo example", output.getvalue())

    def test_typeahead_yes_cannot_approve_a_later_action(self):
        ui = self.app.Terminal(io.StringIO("yes\n"), io.StringIO(), io.StringIO(), interactive=True)
        self.assertEqual(ui.permission({"tool_name":"Bash", "input":{"command":"publish"}})["behavior"], "deny")

    def test_compaction_replaces_context_estimate_but_child_usage_does_not(self):
        ui = self.app.Terminal(io.StringIO(), io.StringIO(), io.StringIO())
        for count in (200000, 5000):
            ui.event({"type":"assistant", "message":{"usage":{"input_tokens":count}, "content":[]}})
        ui.event({"type":"assistant", "parent_tool_use_id":"child", "message":{"usage":{"input_tokens":5}, "content":[]}})
        self.assertEqual(ui.context_tokens, 5000)

    def test_piped_document_is_kept_with_positional_instruction(self):
        from test_transport import PEER
        import json
        import sys
        output = io.StringIO()
        with patch("auto_model.launcher", return_value=[sys.executable, "-u", "-c", PEER]), \
             patch("auto_model.verify_route", return_value={"model":"sonnet", "effort":"high"}), \
             patch("sys.stdin", io.StringIO("CRITICAL_DOCUMENT")), patch("sys.stdout", output):
            self.app.main(["-p", "--no-session-persistence", "--model", "sonnet", "--effort", "high", "Summarize input"])
        self.assertIn("Summarize input", output.getvalue())
        self.assertIn("CRITICAL_DOCUMENT", output.getvalue())

    def test_unknown_interaction_and_canceled_permission_are_denied(self):
        ui = self.app.Terminal(io.StringIO("yes\n"), io.StringIO(), io.StringIO(), interactive=True)
        self.assertEqual(ui.permission({"tool_name":"FutureInteractive", "requires_user_interaction":True,
                                        "input":{}})["behavior"], "deny")
        ui = self.app.Terminal(io.StringIO("yes\n"), io.StringIO(), io.StringIO(), interactive=True)
        self.assertEqual(ui.permission({"tool_name":"Bash", "input":{},
                                        "_is_canceled": lambda: True})["behavior"], "deny")

    def test_cancellation_wakes_waiting_permission_without_another_enter(self):
        from unittest.mock import Mock
        stream = io.StringIO("yes\n")
        stream.isatty = lambda: True
        ui = self.app.Terminal(stream, io.StringIO(), io.StringIO(), interactive=True)
        canceled = Mock(side_effect=[False, False, True])
        with patch("auto_model.select.select", side_effect=[([], [], []), ([], [], []), RuntimeError("still waiting")]):
            result = ui.permission({"tool_name":"Bash", "input":{}, "_is_canceled":canceled})
        self.assertEqual(result["behavior"], "deny")
        self.assertEqual(stream.tell(), 0)

    def test_streamed_text_is_not_printed_twice(self):
        output = io.StringIO()
        ui = self.app.Terminal(io.StringIO(), output, io.StringIO())
        ui.event({"type":"stream_event", "event":{"type":"content_block_delta",
                  "delta":{"type":"text_delta", "text":"hello"}}})
        ui.event({"type":"assistant", "message":{"content":[{"type":"text", "text":"hello"}]}})
        ui.event({"type":"result", "result":"hello", "subtype":"success"})
        self.assertEqual(output.getvalue().strip(), "hello")

    def test_json_print_mode_excludes_unsolicited_background_results(self):
        import json
        output = io.StringIO()
        ui = self.app.Terminal(io.StringIO(), output, io.StringIO(), output_format="json")
        ui.event({"type":"result", "result":"BACKGROUND", "_router_background":True})
        ui.event({"type":"result", "result":"CURRENT"})
        self.assertEqual(json.loads(output.getvalue())["result"], "CURRENT")

    def test_unhandled_control_request_is_not_approved(self):
        ui = self.app.Terminal(io.StringIO(), io.StringIO(), io.StringIO())
        with self.assertRaises(RuntimeError):
            self.app.handle_request({"subtype":"future_control"}, ui, None)

    def test_status_only_exposes_applied_settings_not_credentials(self):
        result = self.app.runtime({"effective": {"env": {"FAKE_SECRET": "fixture"}},
                                   "applied": {"model": "claude-opus-5", "effort": "high"}})
        self.assertEqual(result["model"], "claude-opus-5")
        self.assertNotIn("fixture", str(result))

    def test_effort_mismatch_blocks_execution(self):
        from types import SimpleNamespace
        engine = SimpleNamespace(request=lambda *a, **kw: {"applied": {"model":"claude-opus-5", "effort":"low"}})
        with self.assertRaisesRegex(RuntimeError, "Prompt not sent"):
            self.app.verify_route(engine, SimpleNamespace(model="opus", effort="max"), [])

    def test_agent_presets_retain_effort_when_invocation_overrides_model(self):
        from unittest.mock import MagicMock
        engine = MagicMock()
        self.app.initialize(engine)
        agents = engine.request.call_args.kwargs["agents"]
        self.assertEqual(agents["auto-xs"]["effort"], "low")
        self.assertEqual(agents["auto-xl"]["effort"], "xhigh")

    def test_subagent_usage_does_not_shrink_main_context(self):
        ui = self.app.Terminal(io.StringIO(), io.StringIO(), io.StringIO())
        ui.event({"type":"assistant", "message":{"usage":{"input_tokens":200000}, "content":[]}})
        ui.event({"type":"assistant", "parent_tool_use_id":"child", "message":{"usage":{"input_tokens":10}, "content":[]}})
        self.assertGreaterEqual(ui.context_tokens, 200000)

    def test_running_status_stays_local_and_work_is_queued(self):
        output = io.StringIO()
        ui = self.app.Terminal(io.StringIO("are you working?\nnew task: fix the typo\n"), io.StringIO(), output, interactive=True)
        ui.current_route = "claude-fable-5-1 / xhigh"
        with patch("auto_model.select.select", return_value=([ui.input], [], [])):
            ui.poll_input()
            ui.poll_input()
        self.assertIn("Still running", output.getvalue())
        self.assertIn("fable", output.getvalue())
        self.assertEqual(ui.ask(""), "new task: fix the typo")

    def test_cost_is_reported_without_inventing_savings(self):
        ui = self.app.Terminal(io.StringIO(), io.StringIO(), io.StringIO())
        ui.event({"type":"result", "total_cost_usd":0.123, "result":"ok"})
        self.assertEqual(ui.reported_cost, 0.123)
        ui.event({"type":"result", "total_cost_usd":0, "is_error":True})
        self.assertEqual(ui.reported_cost, 0.123)

    def test_session_task_metadata_roundtrips_privately(self):
        from continuity import TaskState
        from routing import Route
        task = TaskState()
        task.observe("ultrahard task", Route("XL", "fable", "xhigh", "hard", intent="new_task"))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CA_STATE_DIR":directory}):
            session = "18b183e9-65d1-4cf2-a505-2d9de6be9070"
            self.app.save_task(session, task, 12000)
            restored = TaskState()
            self.assertEqual(self.app.restore_task(session, restored), 12000)
            self.assertEqual(restored.task, task.task)
            self.assertEqual(restored.route.model, "fable")
            self.assertEqual((Path(directory) / (session + ".json")).stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.app.restore_task("../../not-a-session", restored), 0)

    def test_main_routes_tasks_not_status_or_short_followups(self):
        from unittest.mock import MagicMock
        from routing import Route
        stream = io.StringIO("ultrahard project\nare you workign?\nyes continue\nnew task: what is HTTP?\n/quit\n")
        ui = self.app.Terminal(stream, io.StringIO(), io.StringIO(), interactive=True)
        engine = MagicMock()
        engine.__enter__.return_value = engine
        engine.session_id = ""
        applied = {"model":"claude-fable-5-1", "effort":"xhigh"}
        calls = []

        def request(subtype, **fields):
            if subtype == "set_model":
                applied["model"] = {"fable":"claude-fable-5-1", "haiku":"claude-haiku-4-5-20251001"}[fields["model"]]
            if subtype == "apply_flag_settings":
                applied["effort"] = fields["settings"]["effortLevel"]
            return {"applied": dict(applied)}

        engine.request.side_effect = request
        engine.turn.side_effect = lambda prompt: calls.append((prompt, dict(applied))) or {"result":"done"}
        classifier = MagicMock()
        classifier.classify.side_effect = [Route("XL","fable","xhigh","hard",intent="new_task"),
                                           Route("XS","haiku","low","easy",intent="new_task")]
        with patch("auto_model.Terminal", return_value=ui), patch("auto_model.Engine", return_value=engine), \
             patch("auto_model.launcher", return_value=["fixture"]), patch("auto_model.initialize", return_value={}), \
             patch("routing.Classifier", return_value=classifier), patch.dict(os.environ, {"CA_STOCK_FUNCTION":"fixture"}), \
             patch("auto_model.select.select", return_value=([stream], [], [])):
            self.assertEqual(self.app.main([]), 0)
        self.assertEqual(classifier.classify.call_count, 2)
        self.assertEqual([model["model"] for _, model in calls],
                         ["claude-fable-5-1", "claude-fable-5-1", "claude-haiku-4-5-20251001"])


if __name__ == "__main__":
    unittest.main()
