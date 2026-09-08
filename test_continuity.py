"""Offline continuity checks with real policy decisions and explicit clock inputs."""

import importlib.util
import time
import unittest

from routing import Route


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("continuity"), "Task continuity is not implemented")
        import continuity
        self.module = continuity
        self.state = continuity.TaskState()
        self.active = Route("XL", "fable", "xhigh", "Novel multi-system engineering")
        self.state.observe("Finish the distributed ledger", self.active, now=100)

    def test_status_and_continuation_phrases_are_anchored_in_english_and_czech(self):
        for prompt in ["are you working?", "are you workign?", "still working", "status",
                       "update me", "Pracuješ?", "pracujes", "Pořád pracuješ?", "Jak to jde?", "Už to máš?"]:
            with self.subTest(status=prompt):
                self.assertTrue(self.module.status_prompt(prompt))
        for prompt in ["yes", "yes continue", "continue", "go on", "do it", "not done", "fix that",
                       "Please continue.", "Ano, pokračuj", "oprav to", "ještě to není hotové"]:
            with self.subTest(continuation=prompt):
                self.assertTrue(self.module.continuation_prompt(prompt))
        for prompt in ["Implement a status page", "Update me on changes in Python", "Continue writing a new poem",
                       "Are you working on a new feature?", "Explain what 'pracujes' means", "Fix that typo in a different project",
                       "Status: add an unrelated new API", "new task: status"]:
            with self.subTest(unrelated=prompt):
                self.assertFalse(self.module.status_prompt(prompt))
                self.assertFalse(self.module.continuation_prompt(prompt))

    def test_twenty_status_followups_keep_fable_effort_and_original_anchor(self):
        for index in range(20):
            prompt = ["are you working?", "are you workign?", "yes continue", "pracujes"][index % 4]
            # Even a mistaken new-task inference cannot turn a status check into a reset.
            proposal = Route("XS", "haiku", "low", "Status check", intent="new_task")
            selected = self.state.select(prompt, proposal, now=101 + index)
            self.assertEqual((selected.tier, selected.model, selected.effort), ("XL", "fable", "xhigh"))
            self.state.observe(prompt, selected, now=101 + index)
        self.assertEqual(self.state.task, "Finish the distributed ledger")
        self.assertIn("Finish the distributed ledger", self.state.context()[0]["content"])
        self.assertEqual(self.state.last_response_at, 120)

    def test_followup_and_uncertainty_hold_the_model_but_can_adjust_effort(self):
        for intent in ["follow_up", "uncertain"]:
            proposal = Route("M", "sonnet", "high", "Additional constraints", intent=intent)
            selected = self.state.select("Also handle duplicate deliveries", proposal, now=1000)
            self.assertEqual((selected.model, selected.effort), ("fable", "high"))
            self.state.observe("Also handle duplicate deliveries", selected, now=1000)
            self.assertEqual(self.state.task, "Finish the distributed ledger")

    def test_genuine_unrelated_task_replaces_anchor_at_small_context(self):
        for prompt, intent in [("What does pwd do?", "new_task"), ("new task: explain pwd", "uncertain")]:
            state = self.module.TaskState(self.active, "Original task", 100)
            selected = state.select(prompt, Route("XS", "haiku", "low", "Lookup", intent=intent),
                                    context_tokens=100, now=101)
            self.assertEqual(selected.model, "haiku")
            self.assertEqual(selected.intent, "new_task")
            state.observe(prompt, selected, now=102)
            self.assertEqual(state.task, prompt)
            self.assertEqual(state.route.model, "haiku")

    def test_escalation_is_allowed_during_continuation(self):
        state = self.module.TaskState(Route("M", "sonnet", "high", "Routine work"), "Build it", 100)
        proposal = Route("XL", "fable", "xhigh", "Severe unresolved debugging", intent="follow_up")
        selected = state.select("Three fixes failed and it still loses data", proposal, context_tokens=500000, now=101)
        self.assertEqual((selected.model, selected.effort), ("fable", "xhigh"))

    def test_same_family_alias_cannot_erase_an_active_version_pin(self):
        state = self.module.TaskState(Route("L", "claude-opus-5", "high", "Active version"), "Build it", 100)
        selected = state.select("continue", Route("L", "opus", "high", "Continuation"), now=1000)
        self.assertEqual(selected.model, "claude-opus-5")

    def test_explicit_aside_is_honored_for_one_turn_without_replacing_active_task(self):
        aside = Route("XS", "haiku", "low", "Explicit aside", True, True)
        selected = self.state.select("Use Haiku at low effort for this turn. A quick aside", aside,
                                     context_tokens=200000, now=101)
        self.assertEqual((selected.model, selected.effort), ("haiku", "low"))
        self.state.observe("Use Haiku at low effort for this turn. A quick aside", selected, now=102)
        self.assertEqual(self.state.task, "Finish the distributed ledger")
        resumed = self.state.select("yes continue", Route("XS", "haiku", "low", "Continue"), now=103)
        self.assertEqual((resumed.model, resumed.effort), ("fable", "xhigh"))
        self.assertFalse(resumed.model_explicit)
        self.assertFalse(resumed.effort_explicit)

    def test_explicit_effort_overrides_short_continuation_without_persisting(self):
        proposal = Route("XS", "haiku", "max", "Explicit effort", False, True)
        selected = self.state.select("continue", proposal, now=101)
        self.assertEqual((selected.model, selected.effort), ("fable", "max"))
        self.state.observe("continue", selected, now=102)
        again = self.state.select("continue", Route("XS", "haiku", "low", "Continue"), now=103)
        self.assertEqual(again.effort, "xhigh")

    def test_large_warm_context_keeps_previous_model_with_proposed_effort(self):
        proposal = Route("S", "sonnet", "low", "New easy edit", intent="new_task")
        selected = self.state.select("Fix the typo in another project", proposal, context_tokens=100000, now=101)
        # Refill premium: 100000 * (1.25*2 - 0.25) = 225000.
        # Estimated output savings: 1000 * (50-10) = 40000, in matching rate units.
        self.assertEqual((selected.model, selected.effort), ("fable", "low"))
        self.assertEqual(selected.intent, "new_task")
        self.assertIn("cache guard", selected.reason)

    def test_cache_guard_uses_output_estimate_and_exact_warm_boundary(self):
        proposal = Route("M", "sonnet", "high", "New feature", intent="new_task")
        # At 140k: 315000 refill premium < 320000 savings. At 250k: 562500 > 320000.
        self.assertEqual(self.state.select("A new feature", proposal, context_tokens=140000, now=400).model, "sonnet")
        self.assertEqual(self.state.select("A new feature", proposal, context_tokens=250000, now=400).model, "fable")
        self.assertEqual(self.state.select("A new feature", proposal, context_tokens=250000, now=400.01).model, "sonnet")
        self.assertEqual(self.state.select("A new feature", proposal, context_tokens=250000, now=99).model, "sonnet")

    def test_cache_prices_distinguish_fable_versions_and_skip_unknown_models(self):
        proposal = Route("M", "sonnet", "high", "New feature", intent="new_task")
        for model, expected in [("claude-fable-5-1", "claude-fable-5-1"), ("claude-fable-5", "sonnet"),
                                ("claude-fable-custom", "sonnet"), ("claude-opus-4-6", "sonnet")]:
            state = self.module.TaskState(Route("XL", model, "xhigh", "Previous"), "Previous task", 100)
            with self.subTest(model=model):
                selected = state.select("A new feature", proposal, context_tokens=200000, now=101)
                self.assertEqual(selected.model, expected)
        unknown_target = Route("M", "claude-sonnet-custom", "high", "Custom deployment", intent="new_task")
        selected = self.state.select("A new feature", unknown_target, context_tokens=500000, now=101)
        self.assertEqual(selected.model, "claude-sonnet-custom")

    def test_manual_overrides_bypass_cache_guard_and_upgrades_are_never_blocked(self):
        for model_explicit, effort_explicit in [(True, False), (False, True)]:
            proposal = Route("M", "sonnet", "high", "Explicit", model_explicit, effort_explicit, "new_task")
            selected = self.state.select("A new feature", proposal, context_tokens=500000, now=101)
            self.assertEqual(selected.model, "sonnet")
        state = self.module.TaskState(Route("M", "sonnet", "high", "Previous"), "Old task", 100)
        self.assertEqual(state.select("new task: severe debugging", self.active, context_tokens=500000, now=101).model,
                         "fable")

    def test_selection_has_no_side_effects_until_response_is_observed(self):
        selected = self.state.select("new task: explain pwd", Route("XS", "haiku", "low", "Lookup"), now=1000)
        self.assertEqual(selected.model, "haiku")
        self.assertEqual((self.state.route, self.state.task, self.state.last_response_at),
                         (self.active, "Finish the distributed ledger", 100))

    def test_default_timestamps_use_wall_clock_for_session_restore(self):
        before = time.time()
        self.state.observe("continue", self.active)
        self.assertGreaterEqual(self.state.last_response_at, before)
        self.assertLessEqual(self.state.last_response_at, time.time())
        restored = self.module.TaskState(self.state.route, self.state.task, self.state.last_response_at)
        selected = restored.select("new task: a routine feature", Route("M", "sonnet", "high", "New feature"),
                                   context_tokens=250000)
        self.assertEqual(selected.model, "fable")

    def test_context_is_bounded_and_preserves_the_active_route(self):
        self.assertEqual(self.module.TaskState().context(), [])
        self.state.task = "Important task " + "x" * 100000
        context = self.state.context()
        self.assertEqual(len(context), 1)
        self.assertLessEqual(len(context[0]["content"]), 2000)
        self.assertTrue(context[0]["content"].startswith("ACTIVE TASK\n"))
        for value in ["Important task", "XL", "fable", "xhigh"]:
            self.assertIn(value, context[0]["content"])


if __name__ == "__main__":
    unittest.main()
