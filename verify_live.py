"""Opt-in paid smoke check: python3 verify_live.py. Uses normal Claude login.

Runs only synthetic routing and memory checks. No project files, tools or MCPs.
"""
import json
from pathlib import Path
import subprocess
import sys

from auto_model import Terminal, handle_request, initialize, launcher, verify_route
from routing import Classifier
from transport import Engine


CASES = [
    ("XS", "What is the git command to show the current branch?"),
    ("L", "Decide whether Postgres or MongoDB is appropriate for our financial ledger and evaluate data integrity."),
    ("M", "Add a dark mode toggle to an existing settings page."),
    ("XL", "Design a novel distributed transaction protocol across three independent payment networks. Previous fixes deadlocked and lost data. This is an ultrahard task requiring the deepest reasoning."),
    ("XS", "What does HTTP stand for?"),
]


def main():
    command = launcher()
    classifier = Classifier(command)
    ui = Terminal()
    outputs = []
    engine_args = ["-p", "--safe-mode", "--setting-sources", "", "--tools", "",
                   "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                   "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                   "--no-session-persistence", "--system-prompt", "Follow the user's instructions. Be brief."]
    with Engine(command + engine_args, on_event=ui.event) as engine:
        caps = engine.request("initialize")
        session_id = None
        for index, (tier, task) in enumerate(CASES):
            route = classifier.classify(task)
            assert route.tier == tier, f"Expected {tier}, got {route}"
            engine.request("set_model", model=route.model)
            engine.request("apply_flag_settings", settings={"effortLevel": route.effort})
            applied = verify_route(engine, route, caps.get("models", []))
            ui.observed_models.clear()
            prompt = ("Remember the marker ROUTER6421. Reply with just that marker." if index == 0 else
                      "What marker did I ask you to remember earlier? Reply with just the marker.")
            result = engine.turn(prompt)
            assert not result.get("is_error"), result.get("subtype")
            assert "ROUTER6421" in result.get("result", ""), "Conversation memory was lost"
            assert applied["model"] in ui.observed_models, f"Response model mismatch: {ui.observed_models}"
            session_id = session_id or result["session_id"]
            assert result["session_id"] == session_id, "Session changed"
            row = {"tier": tier, "model": applied["model"], "effort": applied["effort"],
                   "session_id": session_id, "pid": engine.process.pid, "memory_preserved": True}
            outputs.append(row)
            print(json.dumps(row), flush=True)
    print(f"PASS: {len(outputs)} routed turns, one process, one session, retained conversation memory.")


def check_subagent():
    command = launcher()
    ui = Terminal()
    classifier = Classifier(command)
    updates = []

    def request(req):
        result = handle_request(req, ui, classifier)
        update = result.get("hookSpecificOutput", {}).get("updatedInput")
        if update:
            updates.append(update)
        return result

    flags = ["-p", "--safe-mode", "--setting-sources", "", "--tools", "Agent",
             "--allowedTools", "Agent", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
             "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
             "--no-session-persistence", "--permission-prompt-tool", "stdio", "--forward-subagent-text",
             "--model", "sonnet", "--effort", "high",
             "--system-prompt", "Follow the user exactly. Do not do extra work."]
    with Engine(command + flags, on_event=ui.event, on_request=request) as engine:
        initialize(engine)
        result = engine.turn("Use the Agent tool once with subagent_type general-purpose and no model parameter. "
                             "Delegate exactly this task: Fix the single typo in this supplied text: 'teh dog'. "
                             "Respond with the corrected text only. Then repeat its result.")
        assert not result.get("is_error"), result.get("subtype")
        assert updates, "No subagent routing hook ran"
        assert updates[0]["subagent_type"] == "auto-s", updates[0]
        assert updates[0]["model"] == "sonnet", updates[0]
        assert "the dog" in result.get("result", "").lower(), "Child did not complete its task"
        assert result.get("subagent_stats", {}).get("completed", 0) >= 1, "Subagent did not complete"
        print("PASS: real Agent call routed to auto-s, whose registered definition is Sonnet/low; child completed.")


def check_resume():
    base = [sys.executable, str(Path(__file__).with_name("auto_model.py")), "-p", "--safe-mode",
            "--tools", "", "--model", "sonnet", "--effort", "low", "--output-format", "json"]
    def run(extra):
        result = subprocess.run(base + extra, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    first = run(["My preferred CSS accent color is blue. Give me one CSS custom property for it."])
    second = run(["--resume", first["session_id"], "Which CSS color name did I choose earlier? Reply only with that color name."])
    assert first["session_id"] == second["session_id"], "Resume created a different session"
    assert "blue" in second["result"].lower(), second["result"]
    print(f"PASS: restarted controller resumed session {second['session_id']} with the user's CSS color retained.")


if __name__ == "__main__":
    if "--subagent" in sys.argv:
        check_subagent()
    elif "--resume" in sys.argv:
        check_resume()
    else:
        main()
