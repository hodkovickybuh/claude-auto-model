#!/usr/bin/env python3
"""Automatic model and effort selection around the real Claude Code engine."""
import argparse
from collections import deque
import json
import os
from pathlib import Path
import re
import select
import secrets
import shutil
import subprocess
import sys
import tempfile

from transport import Engine


EFFORTS = ("low", "medium", "high", "xhigh", "max")
HELP = """/model NAME | auto    Pin the model, or release its pin.
/effort LEVEL | auto  Pin effort, or release its pin.
/auto                Release both pins.
/status              Show the engine's actual model and effort.
/models              List models advertised by this Claude Code installation.
/cost                Show Claude's latest reported cost and classifier overhead.
/paste               Enter several lines, ending with a line containing only a dot.
/quit                Exit. The conversation remains resumable with --resume ID.
Ctrl+C               Interrupt the running task.
While running, status questions are local. Other messages queue for the next turn.
Other slash commands are sent to Claude Code; interactive pickers need _claude_stock.
"""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=HELP,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("--model")
    parser.add_argument("--effort", choices=EFFORTS)
    parser.add_argument("-p", "--print", dest="print_mode", action="store_true")
    parser.add_argument("-c", "--continue", dest="continue_session", action="store_true")
    parser.add_argument("-r", "--resume", metavar="SESSION_ID")
    parser.add_argument("--doctor", action="store_true", help="Check CLI control support without inference")
    parser.add_argument("--classify", action="store_true", help="Classify the prompt without executing its task")
    parser.add_argument("--native", action="store_true", help="Run the original Claude terminal")
    parser.add_argument("--classifier-timeout", type=float, default=25)
    parser.add_argument("--output-format", choices=("text", "json", "stream-json"), default="text")
    for name in ("session-id", "name", "permission-mode", "settings", "setting-sources",
                 "tools", "allowedTools", "disallowedTools", "append-system-prompt",
                 "system-prompt", "max-budget-usd", "agent"):
        parser.add_argument("--" + name)
    for name in ("add-dir", "mcp-config", "plugin-dir"):
        parser.add_argument("--" + name, action="append", default=[])
    for name in ("strict-mcp-config", "no-session-persistence", "safe-mode", "verbose"):
        parser.add_argument("--" + name, action="store_true")
    args = parser.parse_args(argv)
    if not 0 < args.classifier_timeout <= 25:
        parser.error("--classifier-timeout must be between 0 and 25 seconds")
    if args.output_format != "text" and not args.print_mode:
        parser.error("--output-format requires --print")
    return args


def launcher():
    # The user's original zsh function is local trusted code, transferred as a
    # function body. Assignment, unlike eval, cannot execute it while copying.
    if os.environ.get("CA_STOCK_FUNCTION"):
        return ["zsh", "-fc", 'functions[_claude_stock]="$CA_STOCK_FUNCTION"; _claude_stock "$@"', "claude"]
    binary = os.environ.get("CA_CLAUDE_BIN") or shutil.which("claude")
    if not binary:
        raise RuntimeError("Claude Code is not installed or is absent from PATH")
    return [binary]


def backend_args(args):
    flags = ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
             "--verbose", "--include-partial-messages", "--permission-prompt-tool", "stdio"]
    if args.continue_session:
        flags.append("--continue")
    if args.resume:
        flags.extend(["--resume", args.resume])
    for name in ("model", "effort", "session-id", "name", "permission-mode", "settings",
                 "setting-sources", "tools", "allowedTools", "disallowedTools",
                 "system-prompt", "max-budget-usd", "agent"):
        value = getattr(args, name.replace("-", "_"))
        if value is not None:
            flags.extend(["--" + name, value])
    for name in ("add-dir", "mcp-config", "plugin-dir"):
        for value in getattr(args, name.replace("-", "_")):
            flags.extend(["--" + name, value])
    for name in ("strict-mcp-config", "no-session-persistence", "safe-mode"):
        if getattr(args, name.replace("-", "_")):
            flags.append("--" + name)
    guidance = Path(__file__).with_name("TIERING.md").read_text()
    if args.append_system_prompt:
        guidance += "\n" + args.append_system_prompt
    flags.extend(["--append-system-prompt", guidance])
    return flags


class Terminal:
    def __init__(self, input_stream=None, output=None, error=None, *, interactive=None, output_format="text"):
        self.input = input_stream or sys.stdin
        self.output = output or sys.stdout
        self.error = error or sys.stderr
        self.interactive = self.input.isatty() if interactive is None else interactive
        self.output_format = output_format
        self.streaming = False
        self.wrote_text = False
        self.context_tokens = 0
        self.observed_models = set()
        self.idle = None
        self.in_permission = False
        self.permission_canceled = lambda: False
        self.queued = deque()
        self.current_route = "initializing"
        self.reported_cost = None
        self.reader = None
        self.bracketed = False

    def poll_input(self):
        from continuity import status_prompt
        if not self.interactive or self.in_permission:
            return
        if not self.reader and not select.select([self.input], [], [], 0)[0]:
            return
        try:
            line = self.reader.read_line() if self.reader else self.ask("", queued=False)
        except EOFError:
            self.interactive = False
            return
        if line is None:
            return
        if status_prompt(line) or line == "/status":
            self.note(f"Still running on {self.current_route}. No model switch or extra inference.")
        elif line == "/interrupt":
            raise KeyboardInterrupt
        elif line.strip():
            self.queued.append(line)
            self.note("Message queued for the next turn. Current task and model are unchanged.")

    def note(self, message):
        print(message, file=self.error, flush=True)

    def __enter__(self):
        if self.interactive and self.input.isatty():
            from terminal_input import TTYInput
            self.reader = TTYInput(self.input, self.error).__enter__()
        if self.interactive and self.error.isatty():
            print("\x1b[?2004h", file=self.error, end="", flush=True)
            self.bracketed = True
        return self

    def __exit__(self, *args):
        if self.reader:
            self.reader.__exit__(*args)
        if self.bracketed:
            print("\x1b[?2004l", file=self.error, end="", flush=True)

    def ask(self, prompt, *, queued=True):
        if self.in_permission and self.permission_canceled():
            raise EOFError
        if queued and self.queued and not self.in_permission:
            return self.queued.popleft()
        print(prompt, file=self.error, end="", flush=True)
        if self.reader:
            while True:
                line = self.reader.read_line(canceled=self.permission_canceled if self.in_permission else lambda: False)
                if line is not None:
                    return line
                if self.idle and not self.in_permission:
                    self.idle()
                select.select([self.input], [], [], 0.15)
        if self.in_permission and self.input.isatty():
            while not select.select([self.input], [], [], 0.15)[0]:
                if self.permission_canceled():
                    self.note("Permission request canceled by Claude.")
                    raise EOFError
        if self.idle and self.interactive and not self.in_permission:
            # Terminal canonical input becomes readable on Enter. While waiting,
            # service background agent callbacks without adding another input reader.
            while not select.select([self.input], [], [], 0.15)[0]:
                self.idle()
        line = self.input.readline()
        if not line:
            raise EOFError
        if "\x1b[200~" in line:
            while "\x1b[201~" not in line:
                part = self.input.readline()
                if not part:
                    raise EOFError
                line += part
            line = line.replace("\x1b[200~", "").replace("\x1b[201~", "")
        return line.rstrip("\r\n")

    def permission(self, request):
        name, data = request.get("tool_name", "tool"), request.get("input", {})
        if request.get("requires_user_interaction") and name != "AskUserQuestion":
            return {"behavior": "deny", "message": "This interaction requires the original Claude terminal"}
        if not self.interactive:
            return {"behavior": "deny", "message": "No interactive user available to approve this tool"}
        self.note(f"\nPermission requested: {name}\n{json.dumps(data, ensure_ascii=False, indent=2)}")
        self.in_permission = True
        self.permission_canceled = request.get("_is_canceled", lambda: False)
        confirmation = "yes " + secrets.token_hex(3)
        try:
            if name == "AskUserQuestion":
                answers = {}
                for question in data.get("questions", []):
                    self.note(question["question"])
                    options = question.get("options", [])
                    for index, option in enumerate(options, 1):
                        self.note(f"  {index}. {option['label']}: {option.get('description', '')}")
                    answer = self.ask("Answer (blank cancels): ")
                    if not answer:
                        return {"behavior": "deny", "message": "User canceled the question"}
                    if answer.isdigit() and 1 <= int(answer) <= len(options):
                        answer = options[int(answer) - 1]["label"]
                    answers[question["question"]] = answer
                if self.ask(f"Confirm these answers. Type {confirmation}: ").strip().lower() == confirmation:
                    return {"behavior": "allow", "updatedInput": {**data, "answers": answers}}
                return {"behavior": "deny", "message": "Answers were not confirmed"}
            if self.ask(f"Allow this exact action? Type {confirmation}: ").strip().lower() == confirmation:
                return {"behavior": "allow", "updatedInput": data}
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            self.in_permission = False
        return {"behavior": "deny", "message": "User did not approve this tool call"}

    def event(self, event):
        background = event.get("_router_background", False)
        if background:
            event = {key: value for key, value in event.items() if key != "_router_background"}
            if self.output_format == "json":
                return
        if self.output_format == "stream-json":
            print(json.dumps(event, ensure_ascii=False), file=self.output, flush=True)
        kind = event.get("type")
        if kind == "stream_event":
            payload = event.get("event", {})
            if payload.get("type") == "message_start":
                self.streaming = False
                if not event.get("parent_tool_use_id"):
                    self._usage(payload.get("message", {}))
            delta = payload.get("delta", {})
            if delta.get("type") == "text_delta":
                self.streaming = self.wrote_text = True
                if self.output_format == "text":
                    print(delta.get("text", ""), file=self.output, end="", flush=True)
        elif kind == "assistant":
            message = event.get("message", {})
            if not event.get("parent_tool_use_id"):
                self._usage(message)
            for block in message.get("content", []):
                if block.get("type") == "text" and not self.streaming:
                    self.wrote_text = True
                    if self.output_format == "text":
                        print(block.get("text", ""), file=self.output, flush=True)
                elif block.get("type") == "tool_use" and self.output_format == "text":
                    self.note(f"\n[tool: {block.get('name', 'unknown')}]")
        elif kind == "result":
            if isinstance(event.get("total_cost_usd"), (int, float)) and not (
                    event.get("is_error") and event["total_cost_usd"] == 0):
                self.reported_cost = event["total_cost_usd"]
            if self.output_format == "json":
                print(json.dumps(event, ensure_ascii=False), file=self.output, flush=True)
            elif self.output_format == "text":
                if not self.wrote_text and event.get("result"):
                    print(event["result"], file=self.output, end="", flush=True)
                print(file=self.output, flush=True)
            if event.get("is_error"):
                self.note("Claude reported an error: " + str(event.get("errors") or event.get("result") or event.get("subtype")))
            self.wrote_text = self.streaming = False

    def _usage(self, message):
        if message.get("model"):
            self.observed_models.add(message["model"])
        usage = message.get("usage", {})
        count = sum(usage.get(key, 0) or 0 for key in
                    ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))
        if count:
            self.context_tokens = count


def handle_request(request, ui, classifier):
    subtype = request.get("subtype")
    if subtype == "can_use_tool":
        return ui.permission(request)
    if subtype == "hook_callback":
        hook = request.get("input", {})
        if hook.get("hook_event_name") == "PreToolUse":
            from routing import route_subagent
            try:
                updated = route_subagent(hook.get("tool_name", ""), hook.get("tool_input", {}), classifier)
            except ValueError as exc:
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                               "permissionDecisionReason": str(exc)}}
            if updated is not None:
                ui.note(f"[auto subagent: {updated.get('model')}, definition {updated.get('subagent_type', 'inherited')}]")
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": updated}}
        return {}
    raise RuntimeError(f"Unsupported host request: {subtype}; no action approved")


def initialize(engine):
    from routing import TIERS
    agents = {
        "auto-" + tier.lower(): {
            "description": f"General-purpose delegated task using automatic {tier} routing.",
            "prompt": "Complete the delegated task. Preserve project instructions and tool permissions. Report evidence and unresolved limitations.",
            "model": model,
            # Haiku ignores effort; retain low in its preset for a caller's
            # explicit non-Haiku model override of that same definition.
            "effort": effort,
        }
        for tier, (model, effort) in TIERS.items()
    }
    return engine.request("initialize", agents=agents, hooks={"PreToolUse": [{
        "matcher": "Agent|Task", "hookCallbackIds": ["auto-subagent"], "timeout": 40,
    }]})


def runtime(settings):
    applied = settings.get("applied")
    if not isinstance(applied, dict) or not applied.get("model"):
        raise RuntimeError("Claude did not report applied settings; update Claude Code (tested on 2.1.263)")
    # Never display effective/sources: they can contain provider credentials.
    return {key: applied.get(key) for key in ("model", "effort", "advisor", "ultracode")}


def verify_route(engine, route, models):
    state = runtime(engine.request("get_settings"))
    row = next((item for item in models if item.get("value") == route.model or
                item.get("resolvedModel") == route.model), {})
    expected = row.get("resolvedModel") or route.model
    actual = state["model"]
    family_match = route.model in ("haiku", "sonnet", "opus", "fable") and f"claude-{route.model}-" in actual
    if actual != expected and not family_match:
        raise RuntimeError(f"Routing did not apply: requested {route.model}, engine reports {actual}. Prompt not sent.")
    if "haiku" not in actual and state["effort"] != route.effort:
        raise RuntimeError(f"Effort did not apply: requested {route.effort}, engine reports {state['effort']}. Prompt not sent.")
    return state


def task_path(session):
    if not session or not re.fullmatch(r"[0-9a-fA-F-]{36}", session):
        return None
    root = Path(os.environ.get("CA_STATE_DIR") or Path.home() / ".local/state/claude-auto-model")
    return root / (session + ".json")


def save_task(session, task, context_tokens):
    from dataclasses import asdict
    path = task_path(session)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {"route": asdict(task.route) if task.route else None, "task": task.task[:8000],
               "last_response_at": task.last_response_at, "last_model": task._last_model,
               "context_tokens": context_tokens}
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".task-", delete=False) as staged:
        json.dump(payload, staged, ensure_ascii=False)
    os.replace(staged.name, path)


def restore_task(session, task):
    from routing import Route, choose_route
    path = task_path(session)
    try:
        if path is None or path.stat().st_size > 65536:
            return 0
        payload = json.loads(path.read_text())
        route = choose_route(Route(**payload["route"]))
        text = payload["task"]
        context = payload["context_tokens"]
        timestamp = payload.get("last_response_at", 0)
        if not isinstance(text, str) or not isinstance(context, int) or context < 0 or not isinstance(timestamp, (int, float)):
            return 0
        task.route, task.task, task.last_response_at = route, text[:8000], timestamp
        last_model = payload.get("last_model")
        task._last_model = last_model if isinstance(last_model, str) else None
        return context
    except (OSError, ValueError, TypeError, KeyError):
        return 0


def doctor(command, ui):
    version = subprocess.run(command + ["--version"], capture_output=True, text=True, timeout=15)
    if version.returncode:
        raise RuntimeError("Claude --version failed")
    ui.note(version.stdout.strip())
    with Engine(command + ["-p", "--safe-mode", "--tools", "", "--input-format", "stream-json",
                           "--output-format", "stream-json", "--verbose"],
                env={key: value for key, value in os.environ.items()
                     if key not in ("ANTHROPIC_MODEL", "CLAUDE_CODE_EFFORT_LEVEL")}) as engine:
        engine.request("initialize")
        settings = engine.request("get_settings")
        before = runtime(settings)
        engine.request("apply_flag_settings", settings={"effortLevel": "low"})
        after = runtime(engine.request("get_settings"))
        if "haiku" not in after["model"] and after["effort"] != "low":
            raise RuntimeError("Effort control was acknowledged but not applied")
        ui.note("Control connection and effort update acknowledged.")
        ui.note("Runtime: " + json.dumps(after, ensure_ascii=False))
        return {"version": version.stdout.strip(), "before": before, "after": after}


def main(argv=None):
    args = parse_args(argv)
    command = launcher()
    if args.native:
        native_args = list(sys.argv[1:] if argv is None else argv)
        native_args.remove("--native")
        os.execvpe(command[0], command + native_args, os.environ)
    ui = Terminal(output_format=args.output_format)
    if args.doctor:
        doctor(command, ui)
        return 0
    from routing import Classifier, Route, choose_route
    from continuity import TaskState, continuation_prompt, status_prompt
    classifier = Classifier(command, timeout=args.classifier_timeout)
    if args.classify:
        from dataclasses import asdict
        print(json.dumps(asdict(classifier.classify(args.prompt or sys.stdin.read())), ensure_ascii=False))
        return 0
    stock_env = {} if os.environ.get("CA_STOCK_FUNCTION") else os.environ
    model_lock = args.model or stock_env.get("ANTHROPIC_MODEL")
    effort_lock = args.effort or stock_env.get("CLAUDE_CODE_EFFORT_LEVEL")
    if effort_lock == "auto":
        effort_lock = None
    prompt = args.prompt
    single = args.print_mode or not ui.interactive
    if single and (prompt is None or not ui.interactive):
        supplied = sys.stdin.read()
        prompt = "\n\n".join(part for part in (prompt, supplied) if part)
    if single and not prompt:
        raise ValueError("Provide a prompt or pipe one on stdin")
    history = []
    task = TaskState()
    with ui, Engine(command + backend_args(args), on_event=ui.event,
                on_request=lambda req: handle_request(req, ui, classifier),
                on_idle=ui.poll_input if ui.interactive and not single else None,
                env={key: value for key, value in os.environ.items()
                     if key not in ("ANTHROPIC_MODEL", "CLAUDE_CODE_EFFORT_LEVEL")}) as engine:
        capabilities = initialize(engine)
        models = capabilities.get("models", [])
        if ui.interactive:
            ui.idle = engine.poll
        if args.resume or args.continue_session:
            restored = runtime(engine.request("get_settings"))
            task.route = Route("L", restored["model"], restored["effort"] or "high",
                               "resumed conversation: preserving initialized model")
            task.task = "Resumed task. Prior routing metadata is unavailable; avoid downgrading short continuations."
            if args.resume:
                ui.context_tokens = restore_task(args.resume, task)
        if ui.interactive and not single:
            ui.note("Automatic model and effort routing enabled. /help for controls, /quit to exit.")
        while True:
            if prompt is None:
                try:
                    prompt = ui.ask("\nyou> ")
                except EOFError:
                    break
            if not prompt.strip():
                prompt = None
                continue
            if prompt in ("/quit", "/exit"):
                break
            if prompt == "/help":
                ui.note(HELP)
            elif prompt == "/status" or (task.route and status_prompt(prompt)):
                ui.note(json.dumps(runtime(engine.request("get_settings")), ensure_ascii=False))
                ui.note(f"Pins: model={model_lock or 'auto'}, effort={effort_lock or 'auto'}; session={engine.session_id}")
                ui.note("No foreground turn is running. Background agents may still be active. No model switch requested.")
            elif prompt == "/models":
                for row in models:
                    ui.note(f"{row.get('value')}: {row.get('resolvedModel') or row.get('displayName', '')}")
            elif prompt == "/cost":
                ui.note(f"Claude-reported cost: {ui.reported_cost if ui.reported_cost is not None else 'not reported yet'} USD")
                ui.note(f"Classifier reported total: {classifier.reported_cost:.6f} USD (failed calls may be unreported). Savings are not measured.")
            elif prompt == "/auto":
                model_lock = effort_lock = None
                ui.note("Model and effort selection are automatic.")
            elif prompt.startswith("/model") or prompt.startswith("/effort"):
                parts = prompt.split()
                if len(parts) != 2 or parts[0] not in ("/model", "/effort"):
                    ui.note("Use /model NAME|auto or /effort LEVEL|auto.")
                elif parts[0] == "/model":
                    if parts[1] != "auto":
                        engine.request("set_model", model=parts[1])
                    model_lock = None if parts[1] == "auto" else parts[1]
                    ui.note(f"Model: {model_lock or 'auto'}")
                elif parts[1] in (*EFFORTS, "auto"):
                    if parts[1] != "auto":
                        engine.request("apply_flag_settings", settings={"effortLevel": parts[1]})
                    effort_lock = None if parts[1] == "auto" else parts[1]
                    ui.note(f"Effort: {effort_lock or 'auto'}")
                else:
                    ui.note("Unknown effort. Choose low, medium, high, xhigh, max, or auto.")
            elif prompt == "/paste":
                ui.note("Paste your prompt. Finish with a line containing only a dot.")
                lines = []
                while True:
                    line = ui.ask("")
                    if line == ".":
                        break
                    lines.append(line)
                prompt = "\n".join(lines)
                continue
            else:
                if not prompt.startswith("/"):
                    ui.note("[auto: selecting model and effort]")
                    if model_lock and effort_lock:
                        proposal = Route("L", model_lock, effort_lock, "explicit session choices", True, True)
                    elif task.route and continuation_prompt(prompt):
                        proposal = task.route
                    else:
                        proposal = classifier.classify(prompt, task.context() + history[-6:], ui.context_tokens)
                    route = task.select(prompt, proposal, ui.context_tokens)
                    route = choose_route(route, model_lock, effort_lock, ui.context_tokens)
                    try:
                        engine.request("set_model", model=route.model)
                    except RuntimeError:
                        # A definite rejection is safe to recover from. A timeout is
                        # ambiguous, so it propagates and closes this engine instead.
                        if model_lock or getattr(route, "model_explicit", False) or route.model not in ("fable", "haiku", "sonnet"):
                            raise
                        route = Route("L", "opus", route.effort if route.effort_explicit else "high",
                                      "Requested automatic model unavailable; using Opus", False, route.effort_explicit,
                                      intent=route.intent)
                        engine.request("set_model", model=route.model)
                    engine.request("apply_flag_settings", settings={"effortLevel": route.effort})
                    state = verify_route(engine, route, models)
                    effort_label = "no effort dial" if "haiku" in state["model"] else state["effort"]
                    ui.note(f"[auto: {route.tier} -> {state['model']} / {effort_label}; {route.reason}]")
                    ui.current_route = f"{state['model']} / {effort_label}"
                try:
                    result = engine.turn(prompt)
                except KeyboardInterrupt:
                    ui.note("\nInterrupting Claude...")
                    engine.request("interrupt")
                    result = engine.finish_turn()
                if prompt in ("/clear", "/reset", "/new") and not result.get("is_error"):
                    task = TaskState()
                    history = []
                    ui.context_tokens = 0
                elif not prompt.startswith("/"):
                    task.observe(prompt, route)
                if not args.no_session_persistence:
                    try:
                        save_task(engine.session_id, task, ui.context_tokens)
                    except OSError as exc:
                        ui.note(f"Routing metadata could not be saved: {exc}. Claude's conversation is unaffected.")
                history.extend([{"role": "user", "content": prompt},
                                {"role": "assistant", "content": str(result.get("result", ""))[-4000:]}])
                history = history[-8:]
                if single:
                    return 1 if result.get("is_error") else 0
            if single:
                break
            prompt = None
        if engine.session_id and not args.no_session_persistence:
            ui.note(f"Session saved: {engine.session_id}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCanceled.", file=sys.stderr)
        sys.exit(130)
    except (RuntimeError, ValueError, TimeoutError, OSError, subprocess.SubprocessError) as exc:
        print(f"auto-model: {exc}", file=sys.stderr)
        sys.exit(1)
