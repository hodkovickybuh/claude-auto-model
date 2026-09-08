"""Offline evaluator checks. Python peers replace paid inference, not routing policy."""

import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_routing import classifier_reply, result


FACT = {"id": "fact", "category": "fact", "prompt": "What does pwd do?",
        "minTier": "XS", "maxTier": "XS", "rationale": "One command definition.",
        "model": "haiku", "effort": "low", "modelExplicit": False, "effortExplicit": False}
ACTIVE = {"initialTask": "Recover the distributed ledger after three failed repairs.",
          "initialRoute": {"tier": "XL", "model": "fable", "effort": "xhigh",
                           "reason": "Unresolved multi-system failure", "intent": "new_task"}}


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("eval_routing"), "evaluator is not implemented")
        import eval_routing
        self.evaluation = eval_routing

    def invoke(self, cases, *args, command=None):
        with tempfile.TemporaryDirectory() as directory:
            corpus = Path(directory) / "cases.json"
            corpus.write_text(json.dumps(cases), encoding="utf-8")
            output = io.StringIO()
            with patch.object(self.evaluation, "CORPUS", corpus), patch("sys.stdout", output), \
                 patch("auto_model.launcher", return_value=command) as launcher:
                code = self.evaluation.main(list(args))
            return code, json.loads(output.getvalue()), launcher.call_count

    def test_mismatch_exits_nonzero_and_report_file_matches_stdout(self):
        command = classifier_reply({**result("L", intent="new_task"), "total_cost_usd": 0.012}).launcher
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            code, report, _ = self.invoke([FACT], "--live", "--output", str(output), command=command)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), report)
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertEqual((report["evaluated_cases"], report["passed"], report["failed"]), (1, 0, 1))
        self.assertEqual(report["mismatches"], 1)
        self.assertAlmostEqual(report["reported_cost_usd"], 0.012)
        self.assertEqual(report["cases"][0]["actual"]["tier"], "L")
        self.assertIn("tier", report["cases"][0]["mismatches"])
        self.assertGreater(report["cases"][0]["latency_seconds"], 0)

    def test_validation_rejects_bad_labels_duplicate_ids_and_broken_context(self):
        invalid = [[], [FACT, FACT], [{**FACT, "prompt": " "}],
                   [{**FACT, "minTier": "XXL"}], [{**FACT, "minTier": "L", "maxTier": "S"}],
                   [{**FACT, "rationale": ""}], [{**FACT, "effort": "huge"}],
                   [{**FACT, "modelExplicit": "false"}], [{**FACT, "model": "$(env)"}],
                   [{**FACT, "contextTokens": True}], [{**FACT, "contextTokens": -1}],
                   [{**FACT, "elapsedSeconds": -1}], [{**FACT, "intent": "maybe"}],
                   [{**FACT, "initialTask": "missing route"}],
                   [{**FACT, "initialRoute": ACTIVE["initialRoute"]}],
                   [{**FACT, **ACTIVE, "initialRoute": {**ACTIVE["initialRoute"], "tier": "bad"}}],
                   [{**FACT, "history": [{"role": "system", "content": "wrong role"}]}],
                   [{**FACT, "history": [{"role": "user", "content": 7}]}],
                   [{**FACT, "maxTire": "XL"}]]
        for cases in invalid:
            with self.subTest(cases=cases):
                code, report, calls = self.invoke(cases, "--live")
                self.assertEqual(code, 2)
                self.assertFalse(report["ok"])
                self.assertEqual(report["evaluated_cases"], 0)
                self.assertEqual(calls, 0, "Invalid labels must fail before discovering a paid launcher")

    def test_default_is_validation_only_and_cannot_start_a_classifier(self):
        with patch("routing.subprocess.Popen", side_effect=AssertionError("Inference is forbidden")):
            code, report, calls = self.invoke([FACT])
        self.assertEqual((code, calls), (0, 0))
        self.assertEqual((report["mode"], report["validated_cases"], report["evaluated_cases"]),
                         ("offline", 1, 0))
        self.assertIsNone(report["pass_rate"])
        self.assertEqual(report["cases"], [])

    def test_tier_range_is_inclusive_but_does_not_excuse_wrong_model_or_effort(self):
        case = {**FACT, "minTier": "S", "maxTier": "L", "model": "sonnet", "effort": "high"}
        command = classifier_reply(result("M", intent="new_task")).launcher
        code, report, _ = self.invoke([case], "--live", command=command)
        self.assertEqual((code, report["passed"], report["pass_rate"]), (0, 1, 1.0))
        code, report, _ = self.invoke([{**case, "model": "opus", "effort": "max"}],
                                    "--live", command=command)
        self.assertEqual(code, 1)
        self.assertEqual(set(report["cases"][0]["mismatches"]), {"model", "effort"})

    def test_classifier_timeout_is_failure_even_when_fallback_tier_is_allowed(self):
        from routing import Classifier
        command = [sys.executable, "-c", "import time; time.sleep(5)"]
        case = {**FACT, "minTier": "L", "maxTier": "L", "model": "opus", "effort": "high"}
        with patch.object(self.evaluation, "Classifier", side_effect=lambda cmd: Classifier(cmd, timeout=0.05)):
            code, report, _ = self.invoke([case], "--live", command=command)
        self.assertEqual((code, report["timeouts"], report["failed"], report["mismatches"]), (1, 1, 1, 0))
        self.assertEqual(report["cases"][0]["error"], "classifier unavailable: timeout")

    def test_invalid_response_is_not_hidden_by_continuity_and_preserves_reported_cost(self):
        case = {**FACT, **ACTIVE, "prompt": "Also account for a stale replica.",
                "minTier": "XL", "maxTier": "XL", "model": "fable", "effort": "xhigh"}
        command = classifier_reply({"total_cost_usd": 0.021, "structured_output": {}}).launcher
        code, report, _ = self.invoke([case], "--live", command=command)
        self.assertEqual((code, report["failed"], report["classifier_errors"]), (1, 1, 1))
        self.assertEqual(report["cases"][0]["actual"]["tier"], "XL")
        self.assertIn("invalid output", report["cases"][0]["error"])
        self.assertAlmostEqual(report["reported_cost_usd"], 0.021)

    def test_local_status_and_continuations_keep_seed_route_without_inference(self):
        for prompt in ("are you workign?", "Pracuješ?", "/status", "yes continue", "Ano, pokračuj"):
            case = {**FACT, **ACTIVE, "prompt": prompt, "minTier": "XL", "maxTier": "XL",
                    "model": "fable", "effort": "xhigh"}
            with self.subTest(prompt=prompt), \
                 patch("routing.subprocess.Popen", side_effect=AssertionError("Must stay local")):
                code, report, _ = self.invoke([case], "--live", command=["unused"])
            self.assertEqual((code, report["classifier_calls"]), (0, 0))
            self.assertEqual(report["cases"][0]["active_task"], ACTIVE["initialTask"])

    def test_anchor_precedes_bounded_history_and_uncertain_followup_keeps_xl(self):
        case = {**FACT, **ACTIVE, "prompt": "What about the other path?", "minTier": "XL",
                "maxTier": "XL", "model": "fable", "effort": "xhigh",
                "history": [{"role": "user", "content": str(i)} for i in range(10)]}
        check = ("assert data['history'][0]['content'].startswith('ACTIVE TASK\\nRecover the distributed ledger')\n"
                 "assert 'ROUTE: XL fable xhigh' in data['history'][0]['content']\n"
                 "assert [x['content'] for x in data['history'][1:]] == ['4', '5', '6', '7', '8', '9']")
        command = classifier_reply(result("M", intent="uncertain"), check=check).launcher
        original = copy.deepcopy(case)
        code, report, _ = self.invoke([case], "--live", command=command)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["cases"][0]["proposal"]["tier"], "M")
        self.assertEqual(report["cases"][0]["actual"]["tier"], "XL")
        self.assertEqual(report["cases"][0]["active_task"], ACTIVE["initialTask"])
        self.assertEqual(case, original)

    def test_new_task_replaces_anchor_and_context_guard_is_applied(self):
        command = classifier_reply(result("XS", intent="uncertain")).launcher
        for tokens, model in [(0, "haiku"), (160000, "sonnet")]:
            case = {**FACT, **ACTIVE, "prompt": "new task: What does pwd do?",
                    "contextTokens": tokens, "model": model}
            with self.subTest(tokens=tokens):
                code, report, _ = self.invoke([case], "--live", command=command)
                self.assertEqual(code, 0, report)
                self.assertEqual(report["cases"][0]["active_task"], case["prompt"])

    def test_limit_preserves_corpus_order_and_evaluated_denominator(self):
        cases = [{**FACT, "id": str(i)} for i in range(3)]
        command = classifier_reply({**result("XS"), "total_cost_usd": 0.01}).launcher
        code, report, _ = self.invoke(cases, "--live", "--limit", "2", "--jobs", "4", command=command)
        self.assertEqual(code, 0, report)
        self.assertEqual((report["validated_cases"], report["selected_cases"], report["evaluated_cases"]), (3, 2, 2))
        self.assertEqual([row["id"] for row in report["cases"]], ["0", "1"])
        self.assertAlmostEqual(report["reported_cost_usd"], 0.02)

    def test_cli_bounds_fail_before_launcher(self):
        for args in (("--jobs", "0"), ("--jobs", "5"), ("--limit", "0"), ("--limit", "-1")):
            with self.subTest(args=args), patch("sys.stderr", io.StringIO()), \
                 patch("auto_model.launcher", side_effect=AssertionError("Must not launch")):
                with self.assertRaises(SystemExit) as caught:
                    self.evaluation.main(list(args))
                self.assertEqual(caught.exception.code, 2)

    def test_real_cli_offline_validates_fixed_corpus_with_no_claude_on_path(self):
        import os
        completed = subprocess.run([sys.executable, str(Path(self.evaluation.__file__))],
                                   env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1",
                                        "CA_CLAUDE_BIN": "/no-paid-evaluation-allowed"},
                                   capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertGreaterEqual(report["validated_cases"], 30)
        self.assertEqual(report["evaluated_cases"], 0)


if __name__ == "__main__":
    unittest.main()
