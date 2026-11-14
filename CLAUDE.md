# SPHEREx Blink Tool — rules for coding agents

**ACTIVE SPRINT (deadline 2026-11-15):** read `tasks/SPRINT.md` FIRST. It overrides the task files and lists what is CUT. Don't build anything marked CUT.

Algorithm specs: `tasks/08-motion.md` (motion search/PM) and `tasks/09-edge.md` (differentiators). Their sign/orientation conventions are non-negotiable.

Read these before touching code:
1. `docs/contracts.md`: data formats, config, API, cache keys. **Authoritative.**
2. The single `tasks/NN-*.md` file you were assigned. Do only that task.
3. `plan.md` only if you need the reasoning behind a decision.

## Hard rules
- **One task file per session.** Do the steps in order. Don't start the next task file.
- **Never change `docs/contracts.md`** or the function signatures listed in a task. If one seems wrong, STOP and report why.
- **[VERIFY] items:** if a value you need is still marked [VERIFY] in `backend/config.py` or `docs/contracts.md`, STOP and report. Never guess instrument facts (pixel scale, flag bits, HDU names, URL formats).
- **No new dependencies** beyond `requirements.txt` unless the task says to add them.
- **No magic numbers.** Constants go in `backend/config.py` (contract C1).
- **Keep changes surgical.** Don't refactor, rename or reformat code outside your task.
- **Tests:** every task lists required tests. Write them, run `pytest -q`, and paste the summary line in your report. Tests that hit the network are marked `@pytest.mark.network` and skipped by default; run them with `pytest -m network`. **Report skipped tests explicitly.**
- **Fail loud:** no bare `except:`, and no swallowing errors. Log failures with the obs_id/URL and count them.
- **Definition of done:** every "Done when" check in the task passes, and you have pasted its output in your report.

## Conventions
- Python 3.14 (not 3.15: no astropy wheel yet). Use `uv`, not pip. Type hints on all public functions, `logging` (never `print`) inside `backend/`. Scripts may print.
- Format with `ruff format`. Lint with `ruff check`.
- Frontend: vanilla ES modules, no build step, no frameworks, no npm.
- Paths: always via `backend/config.py` constants.
- Commands run from the repo root with `.venv` activated: `source .venv/bin/activate`.

## Report format (end of every session)
```
Task: tasks/NN-name.md
Done: <steps completed>
Not done / skipped: <list, with reason>   ← never omit
Tests: <pytest summary line>, skipped: <n and why>
Done-when checks: <each check + output>
Open issues: <anything surprising, anything you had to stop on>
```
