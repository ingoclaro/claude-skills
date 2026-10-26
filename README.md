# claude-skills

Agent skills by Ingo Claro. Each one ships with a graded eval suite, because a
skill that isn't measured drifts the moment the model underneath it changes.

## Skills

### `defer-issues`

Records deferred work — tech debt, refactors, "we should fix this eventually" —
against the code it affects, so it resurfaces when someone next opens that file,
which is the moment the fix is cheapest. File-specific issues become one-line
`TODO [issue-…]` comments linking a record file; issues spanning a whole module
become path-scoped rules in `.claude/rules/` that auto-load on a matching read.
Either way the issue itself lives in one `issues/issue-<id>.md` record — the
comment and the rule are anchors pointing at it, never copies of it.
The skill also covers the other half: what to do when one of those surfaces
mid-task (gate the fix, keep it in a separate commit, verify full resolution
before closing) and how to audit the set.

```bash
npx skills add ingoclaro/claude-skills --skill defer-issues
```

**Why two mechanisms.** A rule is paid for in context on every matching file
read; a comment costs nothing until someone opens the file. So comments are the
default and rules are reserved for issues with no single line to anchor to. The
[evals](evals/defer-issues/) check that the skill picks the right one per
issue.

**Configuration.** `issues_dir`, `issue_label`, and `rewrite_threshold` in the
SKILL.md `metadata:` block. The rules directory (`.claude/rules`) and the index
file (`ISSUES.md`) are fixed.

**Requirements.** Git; `gh` optional (issues are filed to GitHub when a remote
and an authenticated `gh` are present, local-only otherwise).

### `adversarial-review`

Attacks the commit you just made. A background Sonnet reviewer lists the
riskiest suspected bugs, settles each with a repro script it actually runs,
mutation-tests the commit's own tests to find what they let through, and merges
in `/code-review`'s findings, giving one source-tagged list of confirmed bugs, test
gaps, and dismissed items (agreed decisions are not bugs). It works on a throwaway
copy of the repo, so your tree is never touched, and it fixes only what you
pick, in a separate commit.

```bash
npx skills add ingoclaro/claude-skills --skill adversarial-review
```

Invoked as `/adversarial-review [focus areas]`. It needs a clean tree: with
uncommitted changes it stops and asks you to commit or stash first.

**Requirements.** Git and [bun](https://bun.sh) (the mutation script is
TypeScript). It targets the test files the commit changed for JS/TS and Python,
and runs the full suite for other languages.

**What the evals found.** With a strong session model, the skill's edge is not in
*finding* bugs: bare Sonnet 5.5 found every planted one. Where it pulls ahead is
*proving* test gaps (a surviving mutant every time, never editing your tree)
and reporting in a consistent structure. That costs about 3x a plain review.

### `skill-feedback`

Reviews your recent Claude Code sessions for uses of a specific agent or skill
and files structured per-session feedback as GitHub issues — with a synthesized
conversation transcript, session stats (model, CLI version, tool failures,
follow-ups), duplicate detection, and a mandatory redaction pass before anything
is posted.

The premise: the corrections you make mid-session are the highest-signal
evidence of where a skill fails, and they are almost always lost to the session
log.

```bash
npx skills add ingoclaro/claude-skills --skill skill-feedback
```

Invoked as `/skill-feedback`, or triggers on phrases like "review my
deploy-helper sessions" or "file skill feedback".

**Configuration.** Set the two authored defaults in
`skills/skill-feedback/SKILL.md` frontmatter (`metadata:` block) when
installing, or answer when the skill asks:

- `target_name` — the agent or skill to collect feedback about; also used as the
  GitHub label on filed issues
- `feedback_repo` — the `owner/repo` where issues are filed

**Requirements.** `gh` (authenticated, with permission to open issues in
`feedback_repo`), `jq`, and a POSIX shell — macOS/Linux; native Windows is not
supported.

**Privacy.** Session transcripts can contain sensitive content. The skill treats
session data as untrusted, redacts credentials/URLs/personal data before posting
(see SKILL.md step 6c), and warns before filing into a public repo. Review the
drafted issue body when prompted — you are publishing session content.

## Install

```bash
npx skills add ingoclaro/claude-skills            # pick interactively
npx skills add ingoclaro/claude-skills --list     # see what's here
npx skills add ingoclaro/claude-skills --skill '*'
```

Add `-g` to install for your user rather than the current project, `-y` to skip
prompts, and `-a claude-code` to target a specific agent.

### Direct clone

Skills are discovered at `~/.claude/skills/<name>/SKILL.md`, so clone the repo
somewhere neutral and link in the ones you want:

```bash
git clone https://github.com/ingoclaro/claude-skills ~/src/claude-skills
ln -s ~/src/claude-skills/skills/skill-feedback ~/.claude/skills/skill-feedback
```

Restart Claude Code.

## Evals

Every skill has an eval suite at `evals/<name>/` — self-contained (Python
stdlib only, no network), with generated fixtures, a headless runner, and a
programmatic grader. The suites live outside `skills/` on purpose: installing a
skill copies its whole directory, and a consumer shouldn't have to audit test
runners and fixture generators to adopt a SKILL.md.

- [`evals/defer-issues/`](evals/defer-issues/) — six behaviour
  evals graded off the run's git history, plus a separate trigger eval for
  whether the skill loads at all. Every arm is the shipped skill or its absence
  — no frozen copies of older versions — and the suites carry write-ups of the
  two harness bugs they caught: one that made an iteration worthless, one that
  produced a confident and completely wrong finding about the skill.
- [`evals/skill-feedback/`](evals/skill-feedback/) — generated
  fixture session stores and a headless runner.
- [`evals/adversarial-review/`](evals/adversarial-review/) — five behaviour
  evals, each a fixture repo with one planted bug or test gap, graded off the
  transcript and repo state. They include a bug that sits outside the diff and correct
  code whose tests miss one rule. It also has bun unit tests for the mutation script
  (this suite needs bun as well as Python), plus a write-up of how headless
  permission denials and a write-only sandboxed `/tmp` skewed the baseline arm.

Fixtures are generated by each suite's setup scripts rather than checked in, and
run output lands in that suite's `files/` — both gitignored.

## License

MIT — see [LICENSE](LICENSE).
