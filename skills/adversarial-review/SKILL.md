---
name: adversarial-review
description: Adversarial review of the last commit. A background Sonnet reviewer proves each suspected bug with a repro it runs, mutation-tests the commit's own tests, and merges in /code-review's findings, giving one verified, source-tagged list. Use when the user wants the commit they just made attacked, stress-tested or given a deep second-opinion review. Args are the focus areas. Do not use for uncommitted changes, a PR or another branch, or a quick review; use /code-review for those.
argument-hint: "[focus areas]"
allowed-tools: Bash(git rev-parse:*), Bash(git log:*), Bash(git status:*)
metadata:
  author: Ingo Claro
  tags: "code-review, adversarial-review, mutation-testing, bugs"
  version: 1.3.0
---

# Adversarial review

## 1. Pin the commit

This session's tree, captured when the skill loaded:

- Repo root: !`git rev-parse --show-toplevel`
- HEAD: !`git log -1 --format='%h %s'`
- Uncommitted changes (empty means clean): !`git status --porcelain`

- **It must be a repo:** if the repo root is an error, stop and tell the user to restart the session in the repo to review. The reviewer's `/code-review` runs in this session's working directory, so no other repo can be reviewed from here.
- **It must be clean:** `/code-review` reviews uncommitted changes whenever any exist, so if any changes are listed, ask the user to commit or stash them, then rerun `git status --porcelain` to confirm.
- **The review covers `HEAD`:** the commit is the one on the `HEAD` line. If you asked the user to commit, that line is stale: rerun `git log -1 --oneline` and use that commit instead.

Done when the tree is clean and you can name the commit under review.

## 2. Dispatch the reviewer

Pick the focus areas: `$ARGUMENTS`. If that is empty, name the 2 or 3 riskiest parts of the diff yourself.

Start the reviewer in the background with the Agent tool, `subagent_type: "general-purpose"` and `model: "sonnet"`, and this prompt:

```
Read ${CLAUDE_SKILL_DIR}/references/reviewer.md and follow it.
- Skill dir: ${CLAUDE_SKILL_DIR}
- Repo: <the repo root from step 1>
- Commit: <short sha and subject>
- Spec: <the problem statement or card in a few lines, or its path; "none" if there is none>
- Agreed decisions: <deliberate choices from this session; these are not bugs. "none" if there are none>
- Focus areas: <the focus areas you picked>
- Test command: <the project's test command, e.g. `bun test src/` or `pytest -q`>
- Scratch dir: <the scratchpad directory your environment names, or "none">
```

## 3. Present

When the report arrives:

- **Present:** show the report as it came, already merged and verified. Every confirmed finding carries a repro the reviewer ran and the path of its script, so take the repros as proof; if one looks doubtful, rerun its script before presenting it. Keep its source tags, its dismissed items, and any note that `/code-review` or mutation testing did not run.
- **Fix:** ask which findings to fix. Fix only those, in their own commit ("Review fixes: …").

Done when the user has chosen what to fix.
