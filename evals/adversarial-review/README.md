# adversarial-review evals

Two suites, for the two parts of the skill that can fail independently:

- **Behaviour** (`run_evals.py`, `grade_runs.py`): given a commit with a known
  bug planted in it, does a run of the skill find the bug, prove it, name the
  test gap, respect an agreed decision, and refuse a dirty tree? These are headless
  `claude -p` runs inside throwaway fixture repos, graded by code.
- **Mutation script** (`mutate.test.ts`): unit tests for
  `skills/adversarial-review/scripts/mutate.ts`, which the reviewer runs on
  every review. They're cheap and deterministic, so run them after any change to the script.

Stdlib Python, bash and bun only. No API key beyond the `claude` CLI's own auth.

## Behaviour suite

Each eval is a fixture git repo whose `HEAD` is the commit under review, plus
a prompt. `setup_fixtures.sh` is the source of truth for what each repo holds.

| # | Eval | Planted in HEAD | What passing means |
|---|------|-----------------|--------------------|
| 1 | `planted-bug` | `paginate()` counts pages with `Math.floor`, so a partial last page is unreachable and an empty list has no page. The test only checks page 1. | Both bugs are reported, one is demonstrated by running code, a test gap is named, and the repo is untouched |
| 2 | `agreed-decision` | `parseDuration()` never checks that the whole string parsed: `"5x"` is 0, `"1.5h"` is 5h. A bare number meaning seconds is deliberate, and the prompt says so | The validation bug is reported and demonstrated, the agreed decision is dismissed or left out, and the repo is untouched |
| 3 | `dirty-tree` | `planted-bug`'s commit plus an uncommitted edit and an untracked file | The run asks for a commit or stash, starts no reviewer, and leaves the user's changes as they were |
| 4 | `cache-aliasing` | `parseFlags()` gains a cache, and it reads as a pure function. But `effectiveFlags()`, from the previous commit and untouched by the diff, pushes plan defaults into the array it returns, so once that array is the cached one, flags leak between users | The finding ties the cached array to the mutating caller, it's demonstrated by running code, and the repo is untouched |
| 5 | `weak-tests` | Nothing: `formatBytes()` is correct, and its 8 tests look exhaustive. None reaches the rule that a value that would show as `1024.0 KB` moves up to `1.0 MB`, so deleting the rule, or moving its threshold, leaves every test green | The review names that exact gap, a mutant on the rule is shown surviving, and the repo is untouched |

Assertions are checked by code, not a model. `grade_runs.py` reads the run's
repo (compared with its fixture), the transcript's tool calls (subagents
included), and the review text, which is every main-thread assistant message.
The final message alone isn't enough, because the skill presents the report and then asks
which findings to fix. The regexes match ways of naming the one planted bug,
so they measure recall of a known bug, not review quality in general.

### Configs

| Config | Skill | Prompt |
|--------|-------|--------|
| `with_skill` | `~/.claude/skills/adversarial-review`, which must resolve to this checkout | `prompt`: the slash command |
| `without_skill` | none (`--disallowedTools Skill`, which also removes `/code-review`) | `baseline_prompt`: the same request in plain words |
| `opus_reviewer` | the skill with its hard-coded reviewer `model: "sonnet"` changed to `"opus"`, generated from the current `SKILL.md` at run time | `prompt` |

`dirty-tree` asks the baseline something the bare model has no reason to
refuse, so its baseline score reflects the skill's rule more than any
failing of the model. Read it as "what the skill adds", not as a fair contest.

### Running

```bash
./setup_fixtures.sh
python3 run_evals.py files/iteration-1 --runs 3      # 30 runs, about 25 min at the default --concurrency 2
python3 grade_runs.py files/iteration-1
```

The session model defaults to `claude-sonnet-5-5` (`--model`). The reviewer is
Sonnet whatever you pick, since the skill asks for it (`opus_reviewer` swaps in
Opus). Each with_skill run starts a reviewer agent and a forked `/code-review`,
so keep `--concurrency` low.

Each run gets its own `TMPDIR` under its run dir (`--add-dir`'d), because
sandboxes often block `/tmp`, and that's where the reviewer keeps its repro
scripts. Permissions are an allowlist (`git`, `bun`, `mktemp`, file tools), not
`bypassPermissions`. Background agents can't prompt, so a denial is part of
what a real user hits, and `grading.json`'s `diagnostics` lists every one.

### Diagnostics

`grading.json` carries an ungraded `diagnostics` block: whether the reviewer
was dispatched and on which model, which skills were called and from where, how
many times `mutate.ts` ran, the source tags in the report, the
permission denials, and `/tmp` read errors from the sandbox (`tmp_read_errors`). Read it before you edit the skill over a red score,
since a red eval can come from the skill or from the harness.

### What the first smoke run showed

- **`claude -p` waits for the background reviewer.** The run ends with two
  `result` events, one per main-thread turn, and the report is in the second.
- **`/code-review` called from the reviewer runs as a forked review and
  blocks.** It doesn't run in the background. The reviewer's old "start it, then poll with
  `sleep 20`" step assumed otherwise.
- **Background agents' commands get denied unless they fit an allow rule.** Compound
  commands (`D=…; cat > … <<EOF`, `…; echo exit=$?`) were denied even with
  `Bash(bun:*)` allowed, because prefix rules can't match them. The reviewer now runs
  one plain command per call and writes files with the Write tool.

### What the first iteration showed

- **The planted bugs in evals 1-3 are too easy to discriminate.** Bare Sonnet 5.5 finds
  both `paginate` bugs and the `parseDuration` validation bug just by reading. Only
  `asks_commit_or_stash` and `bug_demonstrated` separate the arms. So the
  suite checks the skill's contract. It doesn't yet show that the skill finds
  bugs a plain review misses. `cache-aliasing` was added for that: its bug
  sits outside the diff and only shows when the code is called twice. Bare
  Sonnet 5.5 still found and reproduced it in 3 of 3 runs, because it greps for
  callers and probes on its own. With a model this strong, the skill's edge isn't
  in finding the bug: it's the mutation testing (named surviving mutants), the
  merge with `/code-review`, and the report's structure. No assertion
  scores those yet.
- **`weak-tests` looked like where the skill pulls ahead, but the harness
  caused part of the gap.** Both arms named the untested rollover rule in 3 of 3 runs, and
  the skill proved it every time with a surviving mutant. The baseline proved it once
  (by editing the user's tree and restoring it with `git checkout`). In the
  other two runs it *tried* to mutation-test and was blocked, as the next
  section describes. Rerun with `/tmp` readable and the wider allowlist
  (iteration-2), the baseline still proved it 1/3: one miss was the built-in heredoc
  check blocking its mutation script, and the other never tried. The honest
  claim is that the skill proves test gaps reliably (3/3), while the bare
  model does it sometimes and not cleanly. When it did, it edited the user's
  working tree.
- **Fixtures use fixed commit dates, and the repo check compares content.**
  Rebuilding the fixtures used to change every sha, which made graded runs
  look as if they had changed their repo.
- **Compare cost, not tokens.** `result` usage counts the main thread with
  cache reads, and subagent totals (from `task_progress`) count no cache reads,
  so `total_tokens` makes with_skill look cheaper than the baseline when it costs about 4×
  as much. `timing.json`'s `cost_usd` includes subagents.
- **The grader needed repeated corrections against transcripts.**
  A regex that worked on the smoke run flagged real, unrelated findings as
  raising the agreed decision, counted failed probes as repros and missed real
  ones (a probe test failing as intended, a probe written into the repo). Read the
  evidence for every failure before you believe a score.

### Reviewer model: Opus vs Sonnet (iteration-3)

`sonnet_reviewer` against `with_skill` (Opus), with the session model Sonnet
5.5 in both:

- **Same scores:** 100% on every eval. Both find every planted bug and
  prove the `weak-tests` gap with a surviving mutant, and their mutation testing
  is just as thorough (8.2 vs 8.4 mutants per run).
- **Sonnet is cheaper and faster:** $0.28 vs $0.51 per run (−45%), 57s vs 81s.
- **Opus keeps the report straighter.** Nothing scores this. On `weak-tests`
  (correct code), Sonnet listed the untested rules under "Confirmed issues"
  in 2 of 3 runs, where Opus said "none" in 2 of 3. Source tags were missing in 4/12 Sonnet
  runs vs 2/12 Opus runs.

The skill switched to Sonnet afterwards (v1.3.0), and `reviewer.md` now says
that correct-but-untested code goes under "Test gaps", never "Confirmed
issues". That rule was **not re-measured**: the next run should compare
`with_skill` against `opus_reviewer` and read the `weak-tests` reports. In
iteration-3, `with_skill` was Opus and `sonnet_reviewer` was the variant,
before the default flipped.

## Sandbox and permissions

Two layers sit between a run and the machine, and in iteration-1 both made
the baseline arm look worse than it is:

- **Claude Code's permission checks.** Headless, nothing can approve a
  prompt, so any command that needs one is denied. In an interactive session the user
  would just say yes. In iteration-1, 13 of 15 baseline runs had commands
  denied (29 in all). Most were heredocs with braces ("expansion
  obfuscation"), compound commands with a part outside the allowlist, `$VARS`,
  or `cd` plus a write. The skill's own reviewer had none, because `reviewer.md` tells
  it to use one plain command per call. The allowlist in `run_evals.py` now
  covers the everyday utilities. The built-in checks can't be allowlisted, and
  only `--permission-mode bypassPermissions` lifts them.
- **The nono sandbox.** If the session runs under nono's stock `claude` profile,
  `/tmp` is **write-only** (a profile that adds `/tmp` to its read list fixes it): a file can be written there but not read back,
  and `mkdtemp` appears to succeed. Models write probes to `/tmp` by habit,
  so a probe "succeeds" and then dies with `Module not found`. The runner
  points `TMPDIR` into the run dir, which helps only code that honours it.

After the profile gave `/tmp` read access and the allowlist was widened
(iteration-2), `/tmp` errors went from 6 to 0. Baseline denials went from 29
to 22, and nearly all of those left are the built-in heredoc check. With
those fixes, the baseline went 15/15 on `planted-bug`.

Before reading a delta, check the denials in `grading.json`'s
`diagnostics`, and check transcripts for `EACCES` or `Module not found` on
`/tmp`. A run that was blocked from proving something is a harness result.

## Mutation script tests

```bash
bun test mutate.test.ts      # TMPDIR=<writable dir> if /tmp is blocked
```
