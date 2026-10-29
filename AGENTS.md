# Agent Rules

## Purpose

Own the local engineering contract for this plugin. Framework-level rules for
the host project live in the host's own agent-rules file; this file governs
the plugin subtree only.

## Publishable-surface hygiene

Publishable files must not contain internal process or planning references.
The shipped surface (`commands/`, `api/`, `webui/`, `extensions/`, `scripts/`,
`tests/`, and `README.md`) is public: anything written there ships to the
plugin repository and the plugin hub.

Banned reference classes (anywhere in those files, comments and docstrings
included):

| Class | Examples of the shape |
| --- | --- |
| Acceptance-criteria ids | `AC-12`, `AC-12/AC-13` |
| Requirement ids | `FR7`, `NFR12` |
| Decision-record ids | `ADR-006` |
| Story ids | `Story 4.1`, `story-2-4` |
| Workflow-run ids | `CR 4-2`, `DS-3`, `ER 1` |
| Epic or sprint identifiers | `Epic 4`, `sprint 2` |
| Process vocabulary | `retrospective`, `mutation-kill`, `escalation` |
| Deferred-decision ids | `D-3`, `D-12` |

The rule file itself and the enforcement test that names the banned patterns
are excluded from the scan. Tool index directories (`.venv`, `.git`,
`.serena`, `.neuralmind`, `.desloppify`, `.github`, `.a0proj`,
`__pycache__`, `graphify-out`) are excluded — their artifacts legitimately contain tokens
that look like the banned shapes.

Rewriting a comment or docstring to drop an id is comment-only work: when
re-wording touches a test, keep the assertion exactly as it was and change
only the surrounding prose. Never weaken an assertion to satisfy this rule.

## Verification

The rule is enforced by `tests/test_internal_mentions.py`. Run it directly:

```
.venv/bin/python -m pytest tests/test_internal_mentions.py -q
```

Or grep the surface with the same pattern (must print nothing):

```
grep -rInE 'AC-[0-9]|\b(FR|NFR)[0-9]+|ADR-[0-9]+|\bStory[\s-][0-9]|\b(CR|DS|ER)[\s-][0-9]|\bEpic[ ]+[0-9]|\bsprint\b|\bretrospective\b|\bBMAD\b|\bmutation-kill\b|\bescalation\b|\bD-[0-9]{1,2}\b' \
  --exclude-dir={.venv,.git,.serena,.neuralmind,.desloppify,.github,.a0proj,__pycache__,graphify-out} \
  --exclude=AGENTS.md --exclude=test_internal_mentions.py \
  commands api webui extensions scripts tests README.md
```

## Environment

- All test runs go through the project-scoped `.venv`.
- Framework-importing suites need `PYTHONPATH=/a0`.
