"""Keep unfinished tasks stable across status checks and short continuations."""

from dataclasses import dataclass, field, replace
import re
import time
import unicodedata

from routing import FAMILIES, TIERS, Route, _family, _reason, choose_route


def _words(prompt):
    if not isinstance(prompt, str):
        return ""
    text = "".join(char for char in unicodedata.normalize("NFKD", prompt.casefold())
                   if not unicodedata.combining(char))
    return " ".join(text.strip().strip(".!? ").split())


def status_prompt(prompt: str) -> bool:
    """Match a complete short status check, including common Czech and one typo."""
    return re.fullmatch(
        r"(?:(?:please|prosim|hey)[, ]+)?(?:"
        r"(?:are you |r u )?(?:still )?(?:working|workign)(?: on (?:it|this|that))?|"
        r"status(?: update)?|update me|(?:any )?(?:updates?|progress|news)(?: yet)?|"
        r"what(?:'s| is) (?:the )?status|how(?:'s| is) it going|"
        r"are you (?:there|done)|have you finished|is it done|"
        r"(?:(?:porad|stale|jeste) )?pracujes(?: jeste)?|jak to jde|"
        r"jak jsi na tom|(?:uz )?(?:to mas|mas hotovo|je to hotove)|jsi tam|stav"
        r")(?:[, ]+(?:please|prosim))?", _words(prompt)) is not None


def continuation_prompt(prompt: str) -> bool:
    """Match a complete continuation, not a new task containing the same words."""
    return re.fullmatch(
        r"(?:(?:yes|yep|yeah|ok|okay|no|please|ano|jo|jasne|dobre|prosim)[, ]+)?(?:"
        r"yes|yep|yeah|ok|okay|sure|ano|jo|jasne|dobre|"
        r"continue|go on|do it|proceed|keep going|keep working|carry on|resume|"
        r"finish (?:it|this|that)|(?:it is |it's |still )?not (?:done|finished)|"
        r"fix (?:it|this|that)|try (?:it )?again|do the rest|complete (?:it|this|that)|"
        r"pokracuj(?: dal)?|udelej to|dokonci to|oprav to|(?:jeste to )?neni hotove|"
        r"neni hotovo|zkus to znovu|neprestavej"
        r")(?:[, ]+(?:please|prosim))?", _words(prompt)) is not None


def _intent(prompt, route):
    if re.match(r"\s*new\s+task\s*:", prompt, re.IGNORECASE):
        return "new_task"
    if status_prompt(prompt):
        return "status"
    if continuation_prompt(prompt):
        return "follow_up"
    return route.intent


# Published default-model snapshot, 2026-09-08, USD per million tokens:
# https://platform.claude.com/docs/en/about-claude/pricing
# input, output, five-minute cache read. Writes cost 1.25 * input.
RATES = {"haiku": (1, 5, 0.1), "sonnet": (2, 10, 0.2),
         "opus": (5, 25, 0.5), "fable": (10, 50, 0.25)}
OUTPUT_ESTIMATE = {"XS": 300, "S": 1000, "M": 8000, "L": 16000, "XL": 32000}


def _rates(model):
    model = model.removesuffix("[1m]")
    if model in RATES:
        return RATES[model]
    if model == "claude-fable-5":
        return 10, 50, 1
    current = {"claude-fable-5-1": "fable", "claude-opus-5": "opus",
               "claude-sonnet-5": "sonnet", "claude-haiku-4-5": "haiku",
               "claude-haiku-4-5-20251001": "haiku"}
    return RATES.get(current.get(model))


@dataclass
class TaskState:
    """Call select before a turn and observe after a response. Times are Unix time.

    The active route survives explicit one-turn asides. _last_model separately
    tracks the last observed response, so an aside cannot falsely warm its cache.
    """

    route: Route | None = None
    task: str = ""
    last_response_at: float = 0.0
    _last_model: str | None = field(default=None, init=False, repr=False)

    def select(self, prompt: str, proposal: Route, context_tokens=0, now=None) -> Route:
        now = time.time() if now is None else now
        proposal = choose_route(proposal, context_tokens=context_tokens)
        intent = _intent(prompt, proposal)
        proposal = replace(proposal, intent=intent)
        previous = self.route
        if previous is None:
            return proposal
        old_family, new_family = _family(previous.model), _family(proposal.model)
        rank = {family: index for index, family in enumerate(FAMILIES)}
        if intent != "new_task":
            if not proposal.model_explicit and (
                    old_family not in rank or new_family not in rank or rank[new_family] <= rank[old_family]):
                tier = max((previous.tier, proposal.tier), key=list(TIERS).index)
                proposal = replace(proposal, tier=tier, model=previous.model,
                                   reason=_reason("active task continuity", proposal.reason))
            if (status_prompt(prompt) or continuation_prompt(prompt) or intent == "status") and not proposal.effort_explicit:
                proposal = replace(proposal, effort=previous.effort)

        old_rates, new_rates = _rates(previous.model), _rates(proposal.model)
        if (not proposal.model_explicit and not proposal.effort_explicit
                and old_rates and new_rates and new_rates[0] < old_rates[0]
                and self._last_model in (None, previous.model)
                and 0 <= now - self.last_response_at <= 300):
            # ponytail: one-turn cache heuristic, not a savings guarantee. Replace
            # the snapshot and output estimates with observed provider usage when available.
            refill_premium = context_tokens * (1.25 * new_rates[0] - old_rates[2])
            output_savings = OUTPUT_ESTIMATE[proposal.tier] * (old_rates[1] - new_rates[1])
            if refill_premium > output_savings:
                proposal = replace(proposal, model=previous.model,
                                   reason=_reason("cache guard: estimated refill exceeds one-turn savings", proposal.reason))
        return proposal

    def observe(self, prompt: str, route: Route, now=None):
        self.last_response_at = time.time() if now is None else now
        self._last_model = route.model
        if self.route is None or _intent(prompt, route) == "new_task":
            self.task = prompt.strip()
            self.route = replace(route, model_explicit=False, effort_explicit=False)
        elif not route.model_explicit and not route.effort_explicit:
            self.route = route

    def context(self) -> list[dict]:
        if self.route is None:
            return []
        content = (f"ACTIVE TASK\n{self.task[:1600]}\n"
                   f"ROUTE: {self.route.tier} {self.route.model} {self.route.effort}")
        return [{"role": "user", "content": content[:2000]}]
