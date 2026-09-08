#!/usr/bin/env python3
"""Validate the fixed corpus offline; --live opts into paid classifier evaluation.

Run offline tests separately with: python3 -m unittest discover -v
Live example: python3 eval_routing.py --live --limit 4 --jobs 2 --output report.json
Labels are hand judgments, not measured accuracy. minTier/maxTier are inclusive;
model/effort/intent are exact optional expectations; explicit flags default false.
Each case is independent. initialRoute is supplied context, not a scored answer.
Seed observation is at t=1000; elapsedSeconds defaults to 600 (a cold cache).
No task is executed, so observe records routing only, not a successful answer.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform
import statistics
import sys
import time

from continuity import TaskState, continuation_prompt, status_prompt
from routing import Classifier, EFFORTS, INTENTS, TIERS, Route, choose_route


CORPUS = Path(__file__).with_name("eval_cases.json")
TIER_ORDER = list(TIERS)


def load_cases(path):
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {"id", "category", "prompt", "minTier", "maxTier", "rationale"}
    optional = {"model", "effort", "intent", "modelExplicit", "effortExplicit", "initialTask",
                "initialRoute", "history", "contextTokens", "elapsedSeconds"}
    if not isinstance(cases, list) or not cases:
        raise ValueError("Corpus must be a nonempty list")
    seen = set()
    for index, case in enumerate(cases):
        try:
            if not isinstance(case, dict) or required - case.keys() or case.keys() - required - optional:
                raise ValueError("Missing or unknown case fields")
            for field in required:
                if not isinstance(case[field], str) or not case[field].strip():
                    raise ValueError(f"{field} must be nonempty text")
            if case["id"] in seen:
                raise ValueError("Duplicate case id")
            seen.add(case["id"])
            if TIER_ORDER.index(case["minTier"]) > TIER_ORDER.index(case["maxTier"]):
                raise ValueError("Reversed tier range")
            for field in ("modelExplicit", "effortExplicit"):
                if field in case and type(case[field]) is not bool:
                    raise ValueError(f"{field} must be boolean")
            if "effort" in case and case["effort"] not in EFFORTS:
                raise ValueError("Invalid expected effort")
            if "intent" in case and case["intent"] not in INTENTS:
                raise ValueError("Invalid expected intent")
            if "model" in case:
                normalized = choose_route(Route("M", case["model"], "high", "Validation")).model
                if normalized != case["model"]:
                    raise ValueError("Expected model must be normalized")
            for field in ("contextTokens", "elapsedSeconds"):
                if type(case.get(field, 0)) is not int or case.get(field, 0) < 0:
                    raise ValueError(f"{field} must be a nonnegative integer")
            history = case.get("history", [])
            if not isinstance(history, list) or any(
                    not isinstance(row, dict) or set(row) != {"role", "content"}
                    or row["role"] not in ("user", "assistant") or not isinstance(row["content"], str)
                    for row in history):
                raise ValueError("Invalid history messages")
            if ("initialTask" in case) != ("initialRoute" in case):
                raise ValueError("initialTask and initialRoute must be provided together")
            if "initialRoute" in case:
                if not isinstance(case["initialTask"], str) or not case["initialTask"].strip():
                    raise ValueError("initialTask must be nonempty text")
                seed = choose_route(Route(**case["initialRoute"]))
                if (seed.intent not in INTENTS or not isinstance(seed.reason, str) or not seed.reason.strip()
                        or type(seed.model_explicit) is not bool or type(seed.effort_explicit) is not bool):
                    raise ValueError("Invalid initialRoute metadata")
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"Case {index + 1}: {exc}") from exc
    return cases


def evaluate_case(case, command):
    started = time.perf_counter()
    task = TaskState()
    classifier = Classifier(command)
    tokens = case.get("contextTokens", 0)
    now = 1000 + case.get("elapsedSeconds", 600)
    proposal = route = None
    error = error_kind = None
    source = "classifier"
    called = False
    try:
        if "initialRoute" in case:
            seed = task.select(case["initialTask"], Route(**case["initialRoute"]), tokens, now=1000)
            task.observe(case["initialTask"], choose_route(seed, context_tokens=tokens), now=1000)
        prompt = case["prompt"]
        if prompt == "/status" or (task.route and status_prompt(prompt)):
            source = "local_status"
            route = task.route
            if route is None:
                raise ValueError("Local status has no seeded route to evaluate")
        else:
            if task.route and continuation_prompt(prompt):
                source = "local_continuation"
                proposal = task.route
            else:
                called = True
                proposal = classifier.classify(prompt, task.context() + case.get("history", [])[-6:], tokens)
                if "classifier unavailable:" in proposal.reason:
                    error = "classifier unavailable:" + proposal.reason.split("classifier unavailable:", 1)[1]
                    error_kind = "timeout" if "timeout" in error else "classifier"
            route = task.select(prompt, proposal, tokens, now=now)
            route = choose_route(route, context_tokens=tokens)
            task.observe(prompt, route, now=now)
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        error, error_kind = f"{type(exc).__name__}: {exc}", "routing"
    mismatches = {}
    if route is not None:
        if not TIER_ORDER.index(case["minTier"]) <= TIER_ORDER.index(route.tier) <= TIER_ORDER.index(case["maxTier"]):
            mismatches["tier"] = {"min": case["minTier"], "max": case["maxTier"], "actual": route.tier}
        for label, attribute in (("model", "model"), ("effort", "effort"), ("intent", "intent"),
                                 ("modelExplicit", "model_explicit"), ("effortExplicit", "effort_explicit")):
            if label in case or label.endswith("Explicit"):
                expected, actual = case.get(label, False), getattr(route, attribute)
                if actual != expected:
                    mismatches[label] = {"expected": expected, "actual": actual}
    return {"id": case["id"], "category": case["category"], "passed": route is not None and not error and not mismatches,
            "source": source, "classifier_called": called, "proposal": asdict(proposal) if proposal else None,
            "actual": asdict(route) if route else None, "active_task": task.task, "mismatches": mismatches,
            "error": error, "error_kind": error_kind, "reported_cost_usd": classifier.reported_cost,
            "latency_seconds": round(time.perf_counter() - started, 6)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="Opt into real, potentially paid classification")
    parser.add_argument("--limit", type=int, help="Evaluate the first N cases in fixed corpus order")
    parser.add_argument("--jobs", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--output", type=Path, help="Also write JSON to a new file (never overwrite)")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    started = time.perf_counter()
    report = {"mode": "live" if args.live else "offline", "ok": False, "validated_cases": 0,
              "selected_cases": 0, "evaluated_cases": 0, "passed": 0, "failed": 0, "pass_rate": None,
              "classifier_calls": 0, "classifier_errors": 0, "timeouts": 0, "mismatches": 0,
              "reported_cost_usd": 0.0, "cases": [], "jobs": args.jobs, "python": platform.python_version(),
              "limitations": ["Offline mode validates corpus structure only; run unittest separately.",
                              "Live pass rate is agreement with this small hand-labeled corpus, not general accuracy.",
                              "Initial routes are supplied; no task answers, engine switching, or savings are evaluated.",
                              "Cost is classifier-reported USD only; missing charges and task costs are not measured.",
                              "Classifier calls count classify invocations, which may resolve without inference.",
                              "No session locks or engine availability probe; model aliases and live results may change."]}
    code = 2
    output = None
    try:
        cases = load_cases(CORPUS)
        report["validated_cases"] = len(cases)
        report["corpus_sha256"] = hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        root = Path(__file__).resolve().parent
        report["source_sha256"] = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                                   for name in ("eval_routing.py", "routing.py", "continuity.py", "auto_model.py")}
        cases = cases[:args.limit]
        report["selected_cases"] = len(cases)
        # Validate the destination before a paid run; a typo must not discard paid results.
        if args.output:
            output = args.output.open("x", encoding="utf-8")
        if args.live:
            from auto_model import launcher
            command = launcher()
            with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                report["cases"] = list(pool.map(lambda case: evaluate_case(case, command), cases))
        rows = report["cases"]
        report["evaluated_cases"] = len(rows)
        report["passed"] = sum(bool(row["passed"]) for row in rows)
        report["failed"] = len(rows) - report["passed"]
        report["pass_rate"] = report["passed"] / len(rows) if rows else None
        report["classifier_calls"] = sum(row["classifier_called"] for row in rows)
        report["classifier_errors"] = sum(row["error_kind"] in ("classifier", "timeout") for row in rows)
        report["timeouts"] = sum(row["error_kind"] == "timeout" for row in rows)
        report["mismatches"] = sum(bool(row["mismatches"]) for row in rows)
        report["reported_cost_usd"] = round(sum(row["reported_cost_usd"] for row in rows), 8)
        latencies = [row["latency_seconds"] for row in rows]
        report["latency_seconds"] = {"median": statistics.median(latencies) if rows else None,
                                     "max": max(latencies) if rows else None}
        report["ok"] = report["failed"] == 0
        code = 0 if report["ok"] else 1
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    report["elapsed_seconds"] = round(time.perf_counter() - started, 6)
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if output is not None:
        try:
            with output:
                output.write(text + "\n")
        except OSError as exc:
            report.update(ok=False, error=f"Could not write report: {exc}")
            text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
            code = 2
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
