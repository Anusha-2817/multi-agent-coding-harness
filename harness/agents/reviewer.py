"""The Reviewer: a plan and a diff in, a verdict out. Runs once per attempt.

It reads `plan` and `diff`. Not `repo_path`, not `edits`, and above all not
`test_result` -- invariant 2, enforced by omission in `base.py` rather than by
this agent's good manners. Blind review is what forces it to judge scope and
intent instead of free-riding on the Tester.

**What is actually left for it to judge.** By the time the loop reaches step J,
three failure modes are already gone: the no-edits check has guaranteed the diff
is non-empty, the scope check has guaranteed every path is inside
`plan.target_files`, and the livelock check has guaranteed the diff is not a
repeat. Those are preconditions, not questions. What survives them is everything
`target_files` is too coarse to see -- `target_files` is file-granular and
`steps` is function-granular -- plus everything that is a property of the change
rather than of its location:

- scope creep inside an allowed file: right file, wrong extent
- a different mechanism reaching the same outcome, the sharpest case being a fix
  hardcoded to the values the failing test happens to use
- deletions no step called for
- under-implementation: a step with no hunk
- violations of `plan.constraints`
- reformatting, wholesale rewrites, and elided files

The hardcoded-fix case is where invariant 2 stops being a principle and starts
paying: such a change goes green, so every participant downstream of the Tester
sees a success. This agent is the only one positioned to reject it, precisely
because it has no green light to defer to.

**It must not review the plan, and that is mechanical rather than tidy.** v1
never replans -- routing rejections back to the Planner is a v2 item behind the
scope fence -- so a rejection meaning "this plan is wrong" sends the Implementer
back to implement the same plan from the same reset baseline. The only outcomes
are a wasted attempt or a livelock halt. The system prompt says so, with the
consequence attached.

**`violated_constraints` holds verbatim entries from `plan.constraints`, and
nothing else.** The harness's scope check writes offending *paths* into the same
field on the `ReviewerRejection` it builds, which is not a constraint at all.
That inconsistency is deliberate and is resolved by naming the writer rather than
by making the two agree: consistency is not reachable when one writer has a
rubric to quote and the other has a path to report. The event log distinguishes
them -- `scope_check_failed` against `review_rejected`.
"""

from __future__ import annotations

from harness.agents.base import Agent
from harness.events import EventLog
from harness.llm import LLMClient
from harness.state import Plan, ReviewVerdict

# The output is three small fields, but the reason field carries a hunk-by-hunk
# attribution walk and thinking is drawn from the same budget. Half the
# Implementer's, which has whole files to emit; the same as the Planner's, which
# also thinks hard and writes little.
MAX_TOKENS = 8000

SYSTEM_PROMPT = """\
You review one proposed change to a small Python repository, against the plan
that change was supposed to carry out. You approve it or you reject it.

You are given exactly two things: the plan, and a unified diff. You do not have
the repository, you do not have the test results, and you will not get them.
That is deliberate. A change can make a failing test pass and still be the wrong
change — by hardcoding the values the test happens to use, by deleting the
behaviour that made the test fail, or by fixing something the plan never
mentioned. Nobody downstream is positioned to catch that, because everyone
downstream can see that the tests went green.

Before the diff reached you, the harness already checked mechanically that it is
non-empty, that it touches only files the plan's target_files names, and that it
is not a repeat of a diff already tried. Do not re-check those. They are not
what you are for.

What you are for is three questions.

1. Attribution — every hunk is accounted for.
   Take each hunk in turn and name the numbered step it carries out. A hunk that
   no step accounts for is a change nobody asked for, even in a file the plan
   allowed. Renamed variables, reordered imports, added error handling,
   reformatted lines, tidied comments, an extra function "while we were in
   there" — these are unaccounted hunks.

   Deletions get the same treatment and deserve more suspicion. A removed
   branch, guard, validation, or raise that no step called for is behaviour
   leaving the codebase silently. A hunk that deletes a large block and replaces
   it with nothing, or with a comment saying the rest is unchanged, is a
   truncated file, not a change — reject it.

2. Correspondence — every step is carried out, as written.
   Take each step in turn and find the hunk that does it. A step with no hunk is
   an unfinished change. A step with a hunk that achieves something else is the
   more important case: the plan describes a mechanism, not merely an outcome,
   and a diff that reaches a different outcome by different means has not
   implemented this plan. Watch in particular for a change that special-cases
   specific input values instead of fixing the logic the plan named.

3. Constraints — each one still holds.
   The plan may list constraints. Check each against the diff and reject if any
   is broken. Quote the ones that are broken, word for word as the plan wrote
   them, in violated_constraints. Put nothing else in that field: it is a list
   of the plan's own constraints, not a second place to write prose. Rejecting
   with an empty violated_constraints is normal — most rejections are about
   attribution or correspondence, and a plan is allowed to list no constraints
   at all.

Write your reason first, then decide.

The reason field is where you do the work, and you do it whichever way you are
leaning. Walk the hunks, name the step each one carries out, and name any you
cannot account for. Then walk the steps and name any you cannot find. Then the
constraints. Only then state your verdict. A reason that says "the change looks
correct and matches the plan" is not a review; it is a guess with a sentence in
front of it.

If you reject, the reason is the only thing the implementer will be shown. It
does not get to see the diff it wrote — it is handed your reason, the plan, and
a freshly reset copy of the original files. So an objection it cannot act on is
an objection that will come back to you unchanged. Name the file, name the
function or the region, say what is there that should not be or what is missing,
and say what a correct change would do instead. "This overreaches the plan" is
useless to it. "In pricing/discounts.py, tier_for was changed as step 2
required, but apply_discount was also rewritten to round differently; no step
asks for that — leave apply_discount untouched" is actionable.

Both mistakes are real, and they cost different things.

Approving a change that does not match the plan puts it on disk. Only a human
stands between your verdict and the filesystem, and a human is reading the same
diff you are, with less context about the plan than you have.

Rejecting a change that does match the plan costs one of five attempts, sends
the implementer back to reproduce work it already did correctly, and — because
it is working from the same plan and the same reset files — risks it returning a
nearly identical diff and the run halting outright.

So neither "be strict" nor "be lenient" is the instruction. The instruction is:
reject for something you can point at in the diff, and approve when you cannot.

These are not grounds for rejection, and rejecting for them is a defect:

- Style, naming, formatting, or structure you would have chosen differently,
  where the plan did not ask for anything different.
- The plan being a poor plan, or the fix looking unlikely to work. You do not
  review the plan. Nothing in this system replans, so a rejection meaning "this
  plan is wrong" sends the implementer back to implement that same plan again;
  the only outcome is a wasted attempt or a halted run. A plan you disagree
  with, faithfully implemented, is approved.
- Missing tests, missing docstrings, missing error handling, or anything else
  the plan did not call for. The instruction to the implementer was to make the
  smallest change that satisfies the plan, and it followed it.
- Any doubt you cannot tie to a specific line of the diff.\
"""

# What an empty `steps` or `constraints` list renders as. See `render_plan`.
NONE_STATED = "(none stated)"


def render_plan(plan: Plan) -> str:
    """The plan as a rubric to check against, not as instructions to carry out.

    Deliberately a second implementation rather than an import of
    `implementer.render_plan`. The two differ in two ways, and both follow from
    the difference in role:

    **Steps are numbered and the numbering is load-bearing here.** The
    Implementer numbers them for legibility; this agent is told to *cite* them by
    number, and its reason becomes the Implementer's retry prompt. The number is
    the shared address between the two agents.

    **No section is ever omitted.** `implementer.render_plan` drops an empty
    `Steps:` or `Constraints:` heading, correctly -- a bare heading reads as a
    truncated prompt to someone being told what to do. For a reviewer, "none
    stated" is *information*: it says the channel is empty rather than that the
    section was dropped, which matters when the instruction is to check each
    entry in turn.

    The alternative to duplicating was importing across two agent modules, which
    couples agents that are supposed to know nothing about each other, or a
    shared prompt module, which one function does not earn. Duplication chosen
    with the differences named is the least bad of the three.
    """
    steps = "\n".join(f"{index}. {step}" for index, step in enumerate(plan.steps, start=1))
    targets = "\n".join(f"- {path}" for path in plan.target_files)
    constraints = "\n".join(f"- {item}" for item in plan.constraints)

    sections = [
        f"Summary: {plan.summary}",
        f"Steps:\n{steps or NONE_STATED}",
        f"Target files:\n{targets}",
        f"Constraints:\n{constraints or NONE_STATED}",
    ]
    return "<plan>\n" + "\n\n".join(sections) + "\n</plan>"


def render_user_message(*, plan: Plan, diff: str) -> str:
    """The two contracted fields, tagged.

    Tagged sections for the reason `planner.py` gives about verbatim pytest
    output, only more so: a unified diff is `---`, `+++`, `@@`, and lines
    beginning with `-` and `+`. Every lightweight delimiter collides with it, and
    a diff is the one thing here that must not be cleaned up -- `render_diff`
    passes line endings through untouched precisely so this agent can see them.
    """
    return f"{render_plan(plan)}\n\n<diff>\n{diff}</diff>"


class Reviewer(Agent):
    """Produces `review`. Its contract lives in `CONTRACTS["reviewer"]`.

    `ReviewVerdict` needs no envelope: unlike `list[FileEdit]` it is already an
    object at the schema root, which is what `response_json_schema` accepts.
    """

    name = "reviewer"

    def __init__(self, event_log: EventLog, client: LLMClient) -> None:
        super().__init__(event_log)
        self.client = client

    def _run(self, *, plan: Plan, diff: str) -> ReviewVerdict:
        user = render_user_message(plan=plan, diff=diff)

        self.log(
            event="llm_request",
            payload={"system_chars": len(SYSTEM_PROMPT), "user_chars": len(user)},
        )

        result = self.client.complete_structured(
            system=SYSTEM_PROMPT,
            user=user,
            schema=ReviewVerdict,
            max_tokens=MAX_TOKENS,
        )

        # The call's cost and shape, not the verdict. `agent_produced` from the
        # base class already carries the verdict.
        self.log(event="llm_response", payload=result.log_payload())
        return result.value
