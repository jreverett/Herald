# herald agent instructions

**Using herald to talk to another person's agent:** read [skill/SKILL.md](skill/SKILL.md). That is
the full protocol - sending messages, files and tasks, threading, staying reachable with a
background `herald wait`, claiming items when several sessions run at once, and triage rules for
incoming work. This file exists so harnesses that auto-load `AGENTS.md` find it. Claude Code should
install the skill instead (see `install.sh`).

**Modifying herald itself:** read the Invariants and Item lifecycle sections of
[README.md](README.md) first - they are the protocol, and a change there is a breaking change even
when the tests still pass.

- Run `python3 -m unittest discover -s tests` before committing. Stdlib only; no third-party
  dependencies, matching herald's design. Tests start real daemons on loopback with their own temp
  `HERALD_DIR`, so they never touch a real install.
- A behaviour change needs a test that **fails against the previous implementation**. Prove it by
  stashing the change and running the new test.
- Bump `__version__` in `herald.py` and add a `CHANGELOG.md` entry. State what was broken and the
  consequence, not just what changed.
- `skill/SKILL.md` is behaviour, not documentation. When a change alters what an agent should do,
  the skill must change in the same commit or the fix does not exist in practice.
- Do not add design or planning documents to this repo. Decisions belong in the code, the commit
  message, and `CHANGELOG.md`.
