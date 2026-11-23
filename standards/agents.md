# Agent Standard v0.2

How Claude (and any other model) is used to make things in this repo. Load this before any multi-agent task. It replaces "use whatever agent happens to be selected."

v0.2 (2026-11-23, from the 2026-11-18 check-in): scouts also research how others solved the problem; builders and tacticians move up one model tier for hard work; new advisor and auditor roles; reviews go to a fresh reviewer per batch; fixes consider subtraction first; stalled agents are caught on a schedule; every request is tracked in a task file; only the conductor can override a hook, with a logged reason; the conductor thinks at high or max effort only while planning.

## Core rules

1. **The clearer the goal, the cheaper the agent.** If a task can be written as exact, checkable instructions, it goes to Haiku. If you can't write it that way yet, the task isn't ready to delegate — break it down further first.
2. **Start cheap, escalate on failure.** Try the cheapest tier the work allows. If it fails the pass check twice, rerun it one tier up instead of retrying the same tier. Five Haiku rounds still cost less than one Opus round.
3. **Roles define the type and level of work, not the decisions.** Don't make "wing agent" or "wiring agent" roles; that's the same prompt with different words. The domain goes in the brief.

## Roles

Every step is classed **easy** or **hard** in the agent table. Hard means: a high-stakes or hard-to-undo result (a merge to main, a change to the schema or standards, a part the rest of the plane depends on), a mechanism with moving parts, or an easy-tier attempt that already failed twice. For a hard row, the agent table's Model column names the higher model and the launch sets `model` explicitly; otherwise the model comes from the agent definition in `.claude/agents/`.

| Role | Easy work | Hard work | Does | Never does |
|---|---|---|---|---|
| **Conductor** | Opus | Opus | Owns the goal. Plans (see [Planning](#planning) for its effort levels), declares the agent table, writes every brief, decides from scout and advisor reports, and is the only role that declares a task done or merges. | Writes code, design files or research itself. If it catches itself doing builder work, that's a process miss. |
| **Advisor** | Opus | Opus | On call. When work runs but doesn't match the intent, says what it should do instead and which role should do it. | Edits files or launches agents. |
| **Tactician** | Sonnet | Opus | Turns one fuzzy plan step into exact builder/scout briefs, launches them, collects results, fixes narrowly scoped breakage ("this part is broken, you know what it should do, fix it"). | Makes design decisions that belong to the conductor. |
| **Builder** | Haiku | Sonnet | Executes one narrowly specified task: write this file to this schema, run this command, fix this named failure. | Interprets vague goals. If the brief is ambiguous it stops and reports. |
| **Scout** | Haiku | Sonnet | Researches before and during the work: what exists, how other people tackled the same problem, the best-known way to do it, and what each option costs us. Returns options with evidence (see [Scouting](#scouting)). | Picks the approach. The conductor decides. |
| **Reviewer** | Opus | Opus | Runs the validators, then judges what validators can't check against named criteria in `standards/`. Returns a scorecard. A fresh instance for each batch. | Says "looks good." Every judgment cites a criterion. |
| **Auditor** | Haiku | Haiku | Runs on a schedule during long tasks and reports stalls, loops, broken rules and dropped requests (see [Audits](#audits-and-stalled-agents)). | Fixes anything. It reports to the conductor. |

### Who can launch whom

- Conductor → advisor, tactician, builder, scout, reviewer, auditor.
  - Conductor launches builders/scouts **directly** only when its brief already meets the builder-ready bar below. Otherwise it goes through a tactician.
- Tactician → builder, scout. It may ask the conductor for an advisor.
- Builder, scout, reviewer, advisor, auditor → nobody.

**Nesting.** Claude Code allows subagents to launch subagents, up to 3 levels deep. The tactician definition includes the Agent tool, so a tactician launches its builders and scouts directly. (The 2026-11-11 A-series tactician ran with no launch tool; that setup is gone.)

**Briefing mode** is only the fallback where nesting is disabled: the tactician writes the builder briefs to `plans/<task>/briefs/*.md` and returns; the conductor launches one builder per brief, then hands the results back to the same tactician (SendMessage) to check. A tactician that does the builder work itself must report it as a process miss.

## Planning

The conductor plans before anything is launched, and the plan is where the thinking goes.

**Effort levels.** The conductor raises its effort only for the planning phase, then drops back:

| Phase | Conductor effort |
|---|---|
| Writing or reassessing the plan (failure modes, waves, units, agent table, briefs) | high; max for large or long-running tasks, or after repeated failures |
| Running the plan (launching, reading results, deciding from reports) | medium |

Other roles run at medium unless their step says otherwise; reviewers and advisors may use high. Higher effort costs much more and rarely helps when the goal is clear.

**The plan file** (`plans/<date>-<task>.md`) contains, in this order:

1. **The request** in the user's words, as the start of the [task file](#task-file).
2. **Failure modes** — what is likely to go wrong, thought through before any work starts.
3. **Open decisions and permissions** — every decision the user still has to make, and every permission the run will need, listed up front so the run doesn't stop halfway on a question or a blocked action.
4. **Order** — the hardest or riskiest goal first. For the plane that usually means mechanisms that work and hold together come before cutting weight; a working heavy plane is easier to slim than a light one that doesn't work.
5. **Waves and units** — waves ordered by dependency; each wave split into units, each small enough for one PR. When a unit fails, fix that unit; don't add a new unit to patch around it.
6. **Agent table:**

```
| Step | Role | Tier (easy/hard) | Model | Input | Output | Pass criterion |
```

No agent is launched that isn't in the table. Adding one mid-task, or bumping a step to hard, means updating the table first. When the plan changes, the conductor edits the plan file; it doesn't stack new work on top of the old plan.

## Builder-ready bar

Briefs can go to Haiku only if they include all of:

1. **Exact output** — file path(s) and format (schema version, template name).
2. **Exact inputs** — the files/URLs to use; nothing to "find."
3. **A mechanical pass check** — a command to run (`plane validate`, `pytest`) or template fields that must all be filled.
4. **Stop rule** — what to do when stuck: report the blocker, don't improvise.

## Scouting

The scout's job is to stop us solving in-house a problem other engineers have already solved.

**When to scout:**
- Before the conductor picks an approach for any non-trivial design problem (a flap mechanism, a battery mount, a fold).
- Whenever a fix is needed and the fix isn't obvious: "the wing is too heavy — how have others cut wing mass without adding parts?" Research is repeated at every step, not done once at the start.

**What a scout report contains** (`knowledge/TEMPLATE-approaches.md` → `knowledge/approaches/<topic>.md`; facts about one build or source still use the other `knowledge/` templates):
- The problem as briefed.
- Approaches found — aim for 3 or more where they exist — each with: who did it, link (opened, never invented), evidence that it worked, what it costs us (mass, part count, complexity, build time), and fit with our requirements.
- "None found" is a finding; say so instead of padding.

**Benchmark mode** (hard tier, Sonnet scout): find the top N options for one problem, have Haiku builders build each one as a design variant, run the same checks on all of them, and return a ranked comparison with reasons. The conductor picks.

## Fixing: subtract before adding

Agents fix by adding by default (a fill here, a bracket there), and that pushes us over weight. Before any fix, the brief must make the agent consider, in this order:

1. **Remove** — can the failing part, or what loads it, go away?
2. **Modify** — change the shape, material, size or position of what's there.
3. **Consolidate** — merge parts so one part does two jobs.
4. **Add** — only if 1–3 can't meet the criterion.

The fix report states which option was used and, if it added mass or parts, why 1–3 didn't work. A fix that fails twice is a loop: stop, and the conductor calls an advisor or a scout for how others solved it.

## Reviews

- Reviews run **per batch**, not per item: at the end of each wave of units, or after about 10 builder outputs, and always before a merge.
- Each batch goes to a **fresh** Opus reviewer that hasn't seen earlier batches or reviews, so it doesn't go easy on fixes to its own earlier findings. Give it the plan, the criteria and the batch's diff; for a re-review, also the previous scorecard's blocking issues, so it checks those first. Don't spawn a reviewer per item.
- Reviewer always runs the automated validators first (`plane validate <path> --tier all`) and pastes the scorecard.
- Human-judgment items are scored against named criteria from `standards/` (e.g. `design-standards.yaml#static_margin`), each as PASS / FAIL / N/A with a one-line reason.
- The reviewer also asks, for each change: could this be simpler, and is there anything that could be removed?
- Output format: see `standards/review-scorecard.md`.

## Task file

Long sessions lose requests in compaction. So:

- Every request from the user goes into a checklist in the task's plan file as it arrives, in their words.
- The conductor can't declare the task done until every item is marked **done** (with the commit or file), or **dropped** (with the user's decision). "Dropped by decision" is not "overdue."
- After every compaction, the conductor rereads the plan file before doing anything else.

## Audits and stalled agents

For any task expected to run more than an hour, the conductor schedules an auditor (Haiku) every 30–60 minutes. The auditor reads the plan file, the agent table and recent agent output, and reports in 10 lines or fewer:

- **Stalls** — an agent whose output hasn't changed in 30 minutes. The conductor asks it for status, then stops it and relaunches with a smaller brief.
- **Loops** — any unit whose fix has failed more than twice.
- **Broken rules** — agents not in the table, the conductor writing code, Opus or Sonnet doing builder-ready work, adding before considering subtraction.
- **Dropped requests** — task-file items with no status.
- **Overrides** — new rows in `plans/conductor-edits.md` or the plan's `## Overrides` section, and any hook overridden more than twice.

## How this is enforced

Rules written only in a file get forgotten once there are enough files, so they are also built into the harness. A hook reminds or blocks; it never silently fixes.

### Built

Six agent definitions live in `.claude/agents/`:

| Agent (`subagent_type`) | Default model | May edit |
|---|---|---|
| builder | haiku (sonnet on hard rows) | files its brief names |
| scout | haiku (sonnet on hard rows) | `knowledge/`, `standards/proposals/` only |
| tactician | sonnet (opus on hard rows) | briefs and plan files; launches builders and scouts |
| reviewer | opus | `reviews/` only |
| advisor | opus | nothing (read-only tools) |
| auditor | haiku | nothing (read-only tools) |

Hooks in `.claude/settings.json` call `tools/agent_hooks.py`:

- **Plan gate** (before every Agent launch): the description must start with a step id (e.g. `B15`) from an agent table in a `plans/` file changed in the last 14 days. The role must match `subagent_type`, and the model must match the row (the launch's explicit `model`, else the agent definition's). Otherwise the launch is blocked.
- **Edit gate** (before Edit, Write, NotebookEdit): the conductor may not edit `core/`, `tests/`, `designs/`, `schema/` or `.github/` unless `plans/conductor-edits.md` has a row dated today (UTC) for that file with a reason. Scouts edit only `knowledge/` and `standards/proposals/`. Reviewers edit only `reviews/`. No agent edits `standards/design-standards.yaml` or `.claude/`.
- **Agent log** (after each launch and on subagent stop): one line per event is appended to `plans/agent-log.jsonl`, with role, model, step, plan, description and token usage where reported.
- **CI citation check** (`plane check-citations`, in `validate.yml`): each URL a change adds under `knowledge/` or `standards/` is fetched. A title that does not match its citation fails; an unreachable URL warns.

**Limit:** the hooks apply only to Claude Code sessions started inside this repo. Chat sessions, other tools and other clones are not covered. CI is the only check that runs everywhere.

**Round 13 limits:** each gate runs `python3` and falls back to `python`, and the gate hooks set `onFailure: block`, so a gate that cannot start blocks the call instead of passing it. `tools/agent_hooks.py` and `.claude/` can be edited only by the main session with a dated `plans/conductor-edits.md` row, and never by a subagent. Bash writes are detected only after the fact: a post-Bash hook blocks the next step and logs any file a shell command changed that its caller may not edit, but it cannot undo the write and can also flag a concurrent agent's change.
Plans are dated by filename (`plans/YYYY-MM-DD-*.md`, 14 days), table rows inside code fences or HTML comments do not count, and only the conductor edits `plans/*.md` and `plans/conductor-edits.md`; tacticians write briefs under `plans/<plan-name>/briefs/`.
The block-on-hook-failure safeguard (`onFailure: block`) needs Claude Code v2.1.295 or newer; older clients let a failed hook through, so everyone should update Claude Code.

### Overrides

A hook can be wrong for a case it didn't foresee, and a hook that blocks too often gridlocks the run. So the **conductor**, and only the conductor, may override a hook when it judges the hook wrong for the case at hand:

- **Edit gate:** a row in `plans/conductor-edits.md` with today's date, the file and a real reason. This table is the override log for edits.
- **Plan gate:** update the plan's agent table first; the gate is never bypassed.
- **Any other hook:** one line under `## Overrides` in the plan file: the hook, the reason, and what it did instead.

No other role overrides: a tactician, builder or scout that hits a hook it thinks is wrong stops and reports to the conductor, who decides. The auditor reads both logs, and the same hook overridden more than twice means the hook or this standard needs changing.

### Not built yet

These rules are enforced by the auditor and the reviewer until hooks exist for them:

| When | Check |
|---|---|
| Before a fix is committed | The fix report names remove / modify / consolidate / add, and explains any added mass or parts. |
| After a compaction or session start | Load the plan file and task checklist back into context. |
| When a reply runs past about 20 words | Remind the agent to stay quiet unless it's answering a question. |
| Before a task is marked done | Every task-file item is done or dropped. |

## Communication and handoffs

- **Quiet by default.** Agents report in short updates and only expand when answering a question. The human should be able to see where a run stands at a glance, without reading through logs.
- **Every unit is a PR.** Parallel variants of the same part (several people or agents trying the same wing) are separate PRs that aren't merged until the owner of that part picks which to merge and which to close.
- **Design changes go through the `plane` CLI.** Bringing in or changing a design never requires editing platform code; if it does, the CLI is missing a command and that's a unit of its own.
- **Handoffs.** When a session ends or hands over, it writes a handoff in the plan folder: the user's exact words, the plan, what's done, what's open, and any rulings made along the way. The next session starts from that file, not from memory.

## Cost check

At the end of a task the conductor reports how many agents of each role and model ran, and how many steps were escalated. If Opus or Sonnet did work that met the builder-ready bar, note it as a process miss.
