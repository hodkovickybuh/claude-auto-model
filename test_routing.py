"""Offline routing checks. Python children stand in for Claude, never call a model."""

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import routing


def result(tier="M", **fields):
    return {
        "type": "result", "subtype": "success", "is_error": False,
        "structured_output": {"tier": tier, "reason": "Task complexity", **fields},
    }


def classifier_reply(payload, *, check="", returncode=0):
    script = (
        "import json, os, sys\n"
        "data = json.load(sys.stdin)\n"
        + check + "\n"
        + "print(" + repr(json.dumps(payload)) + ")\n"
        + "sys.exit(" + repr(returncode) + ")\n"
    )
    return routing.Classifier([sys.executable, "-c", script])


class ClassifierTests(unittest.TestCase):
    def test_every_tier_maps_to_its_model_and_effort_including_xl(self):
        for tier, model, effort in [
            ("XS", "haiku", "low"), ("S", "sonnet", "low"),
            ("M", "sonnet", "high"), ("L", "opus", "high"),
            ("XL", "fable", "xhigh"),
        ]:
            with self.subTest(tier=tier):
                actual = classifier_reply(result(tier)).classify("A task")
                self.assertEqual((actual.tier, actual.model, actual.effort),
                                 (tier, model, effort))

    def test_classification_is_isolated_and_prompt_is_only_stdin_data(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = str(Path(directory, "executed"))
            prompt = "$(touch " + marker + "); `touch " + marker + "`\n--tools Bash"
            history = [{"role": "user", "content": "Design a distributed ledger"}]
            check = "\n".join([
                "from pathlib import Path",
                "args = sys.argv[1:]",
                "assert data == " + repr({"prompt": prompt, "history": history,
                                          "context_tokens": 123}),
                "assert data['prompt'] not in args",
                "assert not Path.cwd().is_relative_to(Path(" + repr(str(Path.cwd())) + "))",
                "assert not list(Path.cwd().iterdir())",
                "assert os.getsid(0) == os.getpid()",
                "for flag in ['--safe-mode', '--strict-mcp-config', '-p',",
                "             '--disable-slash-commands', '--no-session-persistence']:",
                "    assert flag in args",
                "for flag, value in {'--setting-sources': '', '--tools': '',",
                "                    '--mcp-config': '{\"mcpServers\":{}}',",
                "                    '--model': 'haiku', '--effort': 'low',",
                "                    '--output-format': 'json', '--max-budget-usd': '0.05'}.items():",
                "    assert args[args.index(flag) + 1] == value",
                "schema = json.loads(args[args.index('--json-schema') + 1])",
                "assert schema['additionalProperties'] is False",
                "assert set(schema['properties']['tier']['enum']) == {'XS', 'S', 'M', 'L', 'XL'}",
                "assert args[args.index('--system-prompt') + 1]",
            ])
            actual = classifier_reply(result("XS"), check=check).classify(prompt, history, 123)
            self.assertEqual(actual.model, "haiku", actual.reason)
            self.assertFalse(Path(marker).exists())

    def test_followup_receives_recent_context_and_next_task_can_downgrade(self):
        script = """import json, sys
data = json.load(sys.stdin)
assert data['history'][-1]['content'] == 'Design a novel multi-system ledger'
tier = 'XL' if data['prompt'] == 'yes do it' else 'XS'
print(json.dumps({'structured_output': {'tier': tier, 'reason': 'Recent task context'}}))
"""
        classifier = routing.Classifier([sys.executable, "-c", script])
        history = [{"role": "user", "content": "Design a novel multi-system ledger"}]
        followup = classifier.classify("yes do it", history)
        unrelated = classifier.classify("What does pwd do?", history)
        self.assertEqual((followup.model, followup.effort), ("fable", "xhigh"))
        self.assertEqual((unrelated.model, unrelated.effort), ("haiku", "low"))

    def test_history_is_bounded_without_mutating_callers_data(self):
        history = [{"role": "user", "content": str(i) + "x" * 10000} for i in range(30)]
        original = json.dumps(history)
        check = """assert len(data['history']) <= 8
assert len(json.dumps(data['history'])) < 20000
assert data['history'][-1]['content'].startswith('29')
assert data['prompt'] == 'Current task stays intact'
"""
        actual = classifier_reply(result(), check=check).classify("Current task stays intact", history)
        self.assertEqual(actual.model, "sonnet", actual.reason)
        self.assertEqual(json.dumps(history), original)

    def test_explicit_model_version_and_effort_are_for_this_turn(self):
        prompt = "Use fable 5.1 at max effort to design this"
        check = "assert data['prompt'] == " + repr(prompt)
        actual = classifier_reply(result("L", model="fable 5.1", effort="max"), check=check).classify(prompt)
        self.assertEqual((actual.model, actual.effort), ("claude-fable-5-1", "max"))
        self.assertTrue(actual.model_explicit)
        self.assertTrue(actual.effort_explicit)
        following = classifier_reply(result("XS", model=None, effort=None)).classify("What is pwd?")
        self.assertEqual((following.model, following.effort), ("haiku", "low"))
        self.assertFalse(following.model_explicit)
        self.assertFalse(following.effort_explicit)

    def test_full_model_ids_are_accepted_but_unknown_version_names_are_not_invented(self):
        actual = classifier_reply(result(model="claude-sonnet-5", effort="medium")).classify("Use this model")
        self.assertEqual((actual.model, actual.effort), ("claude-sonnet-5", "medium"))
        actual = classifier_reply(result(model="haiku 99.1")).classify("A task")
        self.assertEqual((actual.tier, actual.model, actual.effort), ("L", "opus", "high"))
        self.assertIn("classifier unavailable", actual.reason)

    def test_malformed_or_failed_results_use_safe_non_secret_fallback(self):
        invalid = [
            None, [], {}, {"result": "XL"},
            result("UNKNOWN"), result("XS", effort="extreme"),
            result("XS", model="$(env)"), result("XS", reason=123),
            result("XS", reason=""), result("XS", reason="x" * 241),
            result("XS", reason=" " * 241 + "ok"),
            result("XS", reason="\x1b[31msecret"), result("XS", extra="ignored?"),
            {"structured_output": {"tier": "XS"}},
            {**result("XS"), "is_error": True},
            {**result("XS"), "subtype": "error_max_budget_usd"},
            {**result("XS"), "type": "assistant"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                actual = classifier_reply(payload).classify("secret prompt")
                self.assertEqual((actual.tier, actual.model, actual.effort), ("L", "opus", "high"))
                self.assertIn("classifier unavailable", actual.reason)
                self.assertNotIn("secret", actual.reason)
        for code in ["print('not JSON')", "import sys; print('secret-key', file=sys.stderr); sys.exit(1)"]:
            actual = routing.Classifier([sys.executable, "-c", code]).classify("A task")
            self.assertEqual(actual.model, "opus")
            self.assertNotIn("secret-key", actual.reason)
        actual = classifier_reply(result("XS"), returncode=1).classify("A task")
        self.assertEqual(actual.model, "opus")

    def test_unavailable_launcher_fails_closed(self):
        actual = routing.Classifier(["/no-such-routing-test-executable"]).classify("A task")
        self.assertEqual((actual.tier, actual.model, actual.effort), ("L", "opus", "high"))
        self.assertIn("classifier unavailable", actual.reason)

    @unittest.skipUnless(hasattr(os, "fork"), "POSIX process groups required")
    def test_timeout_kills_descendants_and_returns_promptly(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = str(Path(directory, "surviving-child"))
            code = """import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if os.fork() == 0:
    time.sleep(0.8)
    open(%r, 'w').close()
    os._exit(0)
time.sleep(20)
""" % marker
            started = time.monotonic()
            actual = routing.Classifier([sys.executable, "-c", code], timeout=0.15).classify("A task")
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual((actual.tier, actual.model, actual.effort), ("L", "opus", "high"))
            self.assertIn("timeout", actual.reason)
            time.sleep(0.85)
            self.assertFalse(Path(marker).exists(), "Classifier child survived timeout")

    def test_invalid_timeout_cannot_disable_the_deadline(self):
        for timeout in [0, -1, 25.01, float("inf"), float("nan")]:
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                routing.Classifier([sys.executable], timeout=timeout)

    def test_launcher_requires_an_argument_list(self):
        for launcher in ["claude", [], [None], [""]]:
            with self.subTest(launcher=launcher), self.assertRaises(ValueError):
                routing.Classifier(launcher)

    def test_intent_is_validated_defaulted_and_retained_by_policy(self):
        for intent in ["new_task", "follow_up", "status", "uncertain"]:
            with self.subTest(intent=intent):
                route = classifier_reply(result("M", intent=intent)).classify("A task")
                self.assertEqual(route.intent, intent)
                self.assertEqual(routing.choose_route(route, model_lock="opus").intent, intent)
        self.assertEqual(classifier_reply(result()).classify("A task").intent, "uncertain")
        invalid = classifier_reply(result(intent="do_everything")).classify("A task")
        self.assertIn("classifier unavailable", invalid.reason)

    def test_explicit_directives_survive_timeout_invalid_output_and_missing_launcher(self):
        launchers = [[sys.executable, "-c", "import time; time.sleep(10)"],
                     [sys.executable, "-c", "print('invalid')"], ["/no-such-routing-test-executable"]]
        for launcher in launchers:
            for prompt, expected in [
                ("Use Opus 5 for this turn. Finish the task", ("claude-opus-5", "high")),
                ("Use Fable 5.1. Finish the task", ("claude-fable-5-1", "high")),
                ("Use max effort to finish the task", ("opus", "max")),
            ]:
                with self.subTest(launcher=launcher, prompt=prompt):
                    route = routing.Classifier(launcher, timeout=0.05).classify(prompt)
                    self.assertEqual((route.model, route.effort), expected)
                    self.assertEqual(route.effort_explicit, prompt.startswith("Use max"))
                    self.assertEqual(route.model_explicit, not prompt.startswith("Use max"))
                    self.assertEqual(route.intent, "uncertain")
                    self.assertIn("classifier unavailable", route.reason)

    def test_deterministic_directives_win_over_inference(self):
        route = classifier_reply(result("XS", model="haiku", effort="low")).classify(
            "Use Opus 5 at max effort for this turn. Fix it")
        self.assertEqual((route.model, route.effort), ("claude-opus-5", "max"))
        self.assertTrue(route.model_explicit)
        self.assertTrue(route.effort_explicit)

    def test_complete_directive_never_launches_a_paid_classifier(self):
        classifier = routing.Classifier(["must-not-run"])
        with patch("routing.subprocess.Popen", side_effect=AssertionError("Needless paid classification")):
            for prompt, tier, model, effort in [
                ("Use Opus 5 at max effort for this turn. Finish it", "L", "claude-opus-5", "max"),
                ("Use Fable 5.1 at xhigh effort. Finish it", "XL", "claude-fable-5-1", "xhigh"),
                ("Use sonnet at medium effort to finish it", "M", "sonnet", "medium"),
            ]:
                route = classifier.classify(prompt)
                self.assertEqual((route.tier, route.model, route.effort), (tier, model, effort))
                self.assertTrue(route.model_explicit)
                self.assertTrue(route.effort_explicit)
                self.assertEqual(route.intent, "uncertain")
        self.assertEqual(classifier.reported_cost, 0)

    def test_reported_classifier_cost_accumulates_only_actual_packet_costs(self):
        classifier = classifier_reply({**result(), "total_cost_usd": 0.01})
        classifier.classify("First task")
        classifier.classify("Second task")
        self.assertAlmostEqual(classifier.reported_cost, 0.02)
        classifier.classify("Use Opus 5 at max effort. Do it")
        self.assertAlmostEqual(classifier.reported_cost, 0.02)
        failed = classifier_reply({**result(), "is_error": True, "total_cost_usd": 0.004})
        failed.classify("A task")
        self.assertAlmostEqual(failed.reported_cost, 0.004)
        for cost in [None, -1, "0.1", True, float("inf")]:
            classifier = classifier_reply({**result(), "total_cost_usd": cost})
            classifier.classify("A task")
            self.assertEqual(classifier.reported_cost, 0)

    def test_classifier_removes_inherited_model_and_effort_but_preserves_other_env(self):
        check = """assert 'ANTHROPIC_MODEL' not in os.environ
assert 'CLAUDE_CODE_EFFORT_LEVEL' not in os.environ
assert os.environ['ROUTING_AUTH_FIXTURE'] == 'preserved'
"""
        with patch.dict(os.environ, {"ANTHROPIC_MODEL": "fable", "CLAUDE_CODE_EFFORT_LEVEL": "max",
                                     "ROUTING_AUTH_FIXTURE": "preserved"}):
            route = classifier_reply(result("XS"), check=check).classify("A task")
        self.assertEqual(route.model, "haiku", route.reason)

    def test_active_task_context_survives_recent_history_trimming(self):
        anchor = {"role": "user", "content": "ACTIVE TASK\nFinish the distributed ledger\nROUTE: XL fable xhigh"}
        history = [anchor] + [{"role": "user", "content": "status"} for _ in range(20)]
        check = "assert data['history'][0] == " + repr(anchor) + "\nassert len(data['history']) <= 8"
        route = classifier_reply(result("XL", intent="status"), check=check).classify("are you workign?", history)
        self.assertEqual((route.model, route.intent), ("fable", "status"), route.reason)


class ExplicitRequestTests(unittest.TestCase):
    def test_only_clear_leading_directives_are_parsed(self):
        for prompt, expected in [
            ("Use Opus 5 at max effort for this turn. Build it", ("claude-opus-5", "max")),
            ("Use Fable 5.1. Finish it", ("claude-fable-5-1", None)),
            ("Please use sonnet with low effort to fix it", ("sonnet", "low")),
            ("use max effort to investigate", (None, "max")),
            ("Use claude-opus-5 at high effort", ("claude-opus-5", "high")),
            ("New task: Use Opus 5 at max effort. Build it", ("claude-opus-5", "max")),
        ]:
            with self.subTest(prompt=prompt):
                self.assertEqual(routing.explicit_request(prompt), expected)

    def test_model_mentions_quotes_code_and_negations_are_not_directives(self):
        for prompt in [
            '"Use Opus 5 at max effort" is an example',
            "```\nUse Fable 5.1\n```", "> Use Opus 5 at max effort",
            "Explain when to use Opus 5 at max effort", "Compare Opus 5 with Fable 5.1",
            "Do not use Opus 5", "Use Opus 5 as an example in the docs",
            "Use Fable 5.1 as an example in the docs", "Use Sonnet 4.6 in a comparison table",
            "Use Opus 5 in the model comparison table", "Use `Opus 5` as a heading",
            "Use max effort as a label in the UI", "Explain this. Use Opus 5 at max effort.",
        ]:
            with self.subTest(prompt=prompt):
                self.assertEqual(routing.explicit_request(prompt), (None, None))


class PolicyTests(unittest.TestCase):
    def test_locks_apply_independently(self):
        route = routing.Route("M", "sonnet", "high", "Routine feature")
        model = routing.choose_route(route, model_lock="fable")
        effort = routing.choose_route(route, effort_lock="max")
        both = routing.choose_route(route, model_lock="fable 5.1", effort_lock="medium")
        self.assertEqual((model.model, model.effort), ("fable", "high"))
        self.assertEqual((effort.model, effort.effort), ("sonnet", "max"))
        self.assertEqual((both.model, both.effort), ("claude-fable-5-1", "medium"))
        self.assertEqual((model.model_explicit, model.effort_explicit), (True, False))
        self.assertEqual((effort.model_explicit, effort.effort_explicit), (False, True))
        self.assertEqual((both.model_explicit, both.effort_explicit), (True, True))
        self.assertEqual((route.model, route.effort), ("sonnet", "high"))

    def test_natural_language_explicit_flags_survive_policy_and_reject_fallback(self):
        route = routing.Route("XS", "haiku", "max", "Explicit choice", True, True)
        actual = routing.choose_route(route, context_tokens=160000)
        self.assertEqual((actual.model, actual.effort), ("haiku", "max"))
        self.assertTrue(actual.model_explicit)
        self.assertTrue(actual.effort_explicit)
        with self.assertRaisesRegex(ValueError, "[Ee]xplicit.*unavailable"):
            routing.choose_route(route, available_models=["sonnet", "opus"])
        version = routing.Route("XL", "claude-fable-5-1", "xhigh", "Explicit version", True)
        with self.assertRaises(ValueError):
            routing.choose_route(version, available_models=["claude-fable-5", "opus"])

    def test_explicit_effort_survives_an_automatic_model_fallback(self):
        route = routing.Route("XL", "fable", "xhigh", "Explicit effort", False, True)
        actual = routing.choose_route(route, available_models=["opus"])
        self.assertEqual((actual.model, actual.effort), ("opus", "xhigh"))
        self.assertEqual((actual.model_explicit, actual.effort_explicit), (False, True))

    def test_automatic_haiku_context_guard_has_an_exact_boundary(self):
        for model in ["haiku", "claude-haiku-4-5-20251001"]:
            route = routing.Route("XS", model, "low", "Lookup")
            with self.subTest(model=model):
                self.assertEqual(routing.choose_route(route, context_tokens=159999).model, model)
                actual = routing.choose_route(route, context_tokens=160000)
                self.assertEqual((actual.model, actual.effort), ("sonnet", "low"))
                self.assertIn("context", actual.reason)
                self.assertEqual(routing.choose_route(route, model_lock=model, context_tokens=160000).model, model)

    def test_advertised_ids_are_used_without_inventing_capabilities(self):
        route = routing.Route("M", "sonnet", "high", "Routine feature")
        actual = routing.choose_route(route, available_models=["claude-sonnet-4-6", "claude-opus-5"])
        self.assertEqual(actual.model, "claude-sonnet-4-6")
        with self.assertRaises(ValueError):
            routing.choose_route(route, model_lock="fable", available_models=["claude-opus-5"])
        with self.assertRaises(ValueError):
            routing.choose_route(route, model_lock="claude-opus-99", available_models=["claude-opus-5"])
        self.assertEqual(routing.choose_route(route, model_lock="opus", available_models=["claude-opus-5"]).model,
                         "claude-opus-5")

    def test_unavailable_fable_falls_back_only_to_opus_with_a_reason(self):
        route = routing.Route("XL", "fable", "xhigh", "Novel distributed system")
        actual = routing.choose_route(route, available_models=["sonnet", "opus"])
        self.assertEqual((actual.model, actual.effort), ("opus", "high"))
        self.assertIn("fable unavailable", actual.reason)
        self.assertEqual(routing.choose_route(route, effort_lock="max", available_models=["opus"]).effort, "max")
        with self.assertRaises(ValueError):
            routing.choose_route(route, available_models=["haiku", "sonnet"])

    def test_small_task_fallbacks_prefer_sonnet_then_opus(self):
        route = routing.Route("XS", "haiku", "low", "Lookup")
        self.assertEqual(routing.choose_route(route, available_models=["opus", "sonnet"]).model, "sonnet")
        self.assertEqual(routing.choose_route(route, available_models=["opus"]).model, "opus")
        with self.assertRaises(ValueError):
            routing.choose_route(route, available_models=[])

    def test_restrictions_never_force_a_large_context_onto_haiku(self):
        route = routing.Route("XS", "haiku", "low", "Lookup")
        with self.assertRaises(ValueError):
            routing.choose_route(route, context_tokens=160000, available_models=["haiku"])
        actual = routing.choose_route(route, context_tokens=200000,
                                      available_models=["claude-haiku-4-5-20251001", "claude-opus-5"])
        self.assertEqual(actual.model, "claude-opus-5")
        self.assertIn("context", actual.reason)

    def test_invalid_effort_lock_is_rejected(self):
        with self.assertRaises(ValueError):
            routing.choose_route(routing.Route("XS", "haiku", "low", "Lookup"), effort_lock="huge")

    def test_picker_entries_do_not_hide_advertised_family_and_context_variants(self):
        route = routing.Route("M", "sonnet", "high", "Routine work")
        actual = routing.choose_route(route, available_models=["default", "best", "sonnet[1m]"])
        self.assertEqual(actual.model, "sonnet[1m]")
        actual = routing.choose_route(route, model_lock="sonnet[1m]", available_models=["sonnet[1m]"])
        self.assertEqual(actual.model, "sonnet[1m]")


class SubagentTests(unittest.TestCase):
    def test_unrelated_tools_empty_prompts_and_resumes_do_not_classify(self):
        classifier = routing.Classifier(["must-not-run"])
        with patch("routing.subprocess.Popen", side_effect=AssertionError("Unexpected classification")):
            for name, data in [
                ("Bash", {"prompt": "A task"}), ("TaskCreate", {"prompt": "A task"}),
                ("mcp__x__Agent", {"prompt": "A task"}), ("agent", {"prompt": "A task"}),
                ("Agent", {}), ("Agent", {"prompt": " "}), ("Agent", {"prompt": 42}),
                ("Task", {"prompt": "Continue", "resume": "agent-123"}),
                ("Agent", {"prompt": "A task", "subagent_type": "fork"}),
            ]:
                with self.subTest(name=name, data=data):
                    self.assertIsNone(routing.route_subagent(name, data, classifier))

    def test_delegated_task_changes_only_supported_routing_fields(self):
        for name in ["Agent", "Task"]:
            original = {"prompt": "Solve a novel multi-system problem", "description": "Investigate",
                        "subagent_type": "general-purpose", "run_in_background": True,
                        "isolation": "worktree"}
            check = "assert data['prompt'] == " + repr(original["prompt"]) + "\nassert not data['history']"
            actual = routing.route_subagent(name, original, classifier_reply(result("XL"), check=check))
            self.assertEqual(actual, {**original, "model": "fable", "subagent_type": "auto-xl"})
            self.assertNotIn("model", original)
            self.assertNotIn("permissionDecision", actual)

    def test_explicit_subagent_fields_are_preserved_independently(self):
        classifier = classifier_reply(result("XS"))
        model = {"prompt": "Find a file", "model": "opus"}
        self.assertEqual(routing.route_subagent("Agent", model, classifier),
                         {**model, "subagent_type": "auto-xs"})
        effort = {"prompt": "Find a file", "effort": "max"}
        self.assertEqual(routing.route_subagent("Task", effort, classifier),
                         {**effort, "model": "haiku", "subagent_type": "auto-xs"})
        both = {"prompt": "Find a file", "model": "claude-opus-5", "effort": "high"}
        self.assertEqual(routing.route_subagent("Agent", both, classifier),
                         {**both, "subagent_type": "auto-xs"})
        specialized = {**both, "subagent_type": "security-reviewer"}
        with patch("routing.subprocess.Popen", side_effect=AssertionError("Explicit choices need no inference")):
            self.assertIsNone(routing.route_subagent("Agent", specialized, classifier))

    def test_subagent_model_version_is_not_silently_reduced_to_a_family(self):
        original = {"prompt": "Use fable 5.1 at max effort to design this"}
        classifier = classifier_reply(result("L", model="claude-fable-5-1", effort="max"))
        with self.assertRaisesRegex(ValueError, "version"):
            routing.route_subagent("Agent", original, classifier)

    def test_generic_presets_choose_different_registered_effort_for_same_family(self):
        for current_type in [None, "general-purpose", "auto-xl"]:
            for tier, preset, effort in [("S", "auto-s", "low"), ("M", "auto-m", "high")]:
                with self.subTest(current_type=current_type, tier=tier):
                    original = {"prompt": "A task"}
                    if current_type is not None:
                        original["subagent_type"] = current_type
                    actual = routing.route_subagent("Agent", original, classifier_reply(result(tier)))
                    self.assertEqual(actual, {**original, "model": "sonnet", "subagent_type": preset})
                    self.assertEqual(routing.TIERS[actual["subagent_type"][5:].upper()][1], effort)

    def test_specialized_agent_retains_its_type_and_frontmatter_effort(self):
        original = {"prompt": "Review security", "subagent_type": "security-reviewer"}
        actual = routing.route_subagent("Agent", original, classifier_reply(result("L")))
        self.assertEqual(actual, {**original, "model": "opus"})
        self.assertNotIn("effort", actual)

    def test_effort_override_uses_a_matching_preset_or_fails_clearly(self):
        original = {"prompt": "Use low effort for this task"}
        actual = routing.route_subagent("Agent", original, classifier_reply(result("L", effort="low")))
        self.assertEqual(actual["model"], "opus")
        self.assertEqual(routing.TIERS[actual["subagent_type"][5:].upper()][1], "low")
        with self.assertRaisesRegex(ValueError, "effort"):
            routing.route_subagent("Agent", {"prompt": "Use max effort for this task"},
                                   classifier_reply(result("L", effort="max")))


if __name__ == "__main__":
    unittest.main()
