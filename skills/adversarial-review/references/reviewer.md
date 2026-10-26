# Adversarial reviewer

You review one commit adversarially.

- **Inputs:** the skill dir, repo, commit, spec, agreed decisions, focus areas, test command and scratch dir are all in the prompt that pointed you here.
- **Where to work:** run every command from the repo.
- **The repo is read-only:** put every file you write in one review directory, made with `mktemp -d -p <scratch dir>`, or plain `mktemp -d` if the scratch dir is "none". Sandboxes often block `/tmp`, so don't use it when a scratch dir is given. Keep the directory when you finish: the report points at the repro scripts in it.
- **Nobody can approve a prompt:** you run in the background, so a tool call that needs permission is denied, not asked. Permission rules match a command by its prefix, so keep each Bash call to one plain command: no `;`, `&&`, variable assignments or heredocs. Write files with the Write tool, and read exit codes from the tool result rather than `echo $?`. If a command is still denied, name it in the report instead of trying variants.

## 1. Start /code-review

First, call the Skill tool with `skill: "code-review"` and `args: "<sha>~1..<sha>"`, where `<sha>` is the commit's sha from the prompt. With no args it reviews the whole branch against its upstream or main, not this one commit, and nothing at all once the commit is pushed. If `git rev-parse <sha>~1` fails, the commit has no parent: pass no args.

- **Its result:** it usually runs as a forked review and returns its findings when it finishes; keep them for step 3. If it returns only a task id instead, the findings arrive later as a task notification, and step 5 collects them.
- **If it can't run:** if the Skill tool or `code-review` is unavailable, note "/code-review unavailable" and carry on.

## 2. Suspect

Read `git show HEAD` and the changed files in full. List up to 8 suspected bugs, weighted toward the focus areas:
- boundary conditions
- state left inconsistent when something throws
- unvalidated input
- complexity worse than claimed, only when a focus area names it
- weak tests

Done when each focus area has at least one suspicion, or one line saying why it has none.

## 3. Prove

1. **Merge in /code-review:** if its findings have arrived, add each one to the list, tagged `code-review`. A suspicion of yours that it also names becomes one item, tagged `both`.
2. **Dismiss:** check each item against the agreed decisions. If it matches one, move it straight to "dismissed".
3. **Repro:** settle every remaining item with one repro script in the project's language. Give it one case per item, each printing its input and output, and run it once. A complexity suspicion gets a single case with a deliberately slow input.
   - **Proven:** it becomes a finding, with its input → wrong output.
   - **Not proven:** it goes under "disproved or unproven".

Done when every item is either dismissed, proven, or listed as unproven.

## 4. Mutation testing

The mutants run against only the test files the commit added or changed, so put every mutant in code those tests exercise. A mutant in code they never call survives whatever the tests are like, so it says nothing about them.

- **Find the targets:** list the commit's test files (`git show --name-only HEAD`), and read what each one calls.
- **Place the mutants:** in lines those tests reach, preferably ones the commit changed, weighted toward the focus areas. If the commit touches no test files, the full suite runs instead, so place them in the code the commit changed.

Write 5 to 10 mutants (plausible bugs) as JSON: `[{ "name", "file", "find", "replace" }]`. Each `find` must match exactly once in its file. Then run:

```
bun <skill dir>/scripts/mutate.ts <mutants.json> --cmd "<test command>" --tmp <review dir>
```

- **`--cmd`:** give it without paths (`bun test`, `pytest -q`). The script appends the commit's test files, and the first line of its output names them. A wrapper like `npm test` needs a trailing `--` (`npm test --`) so the files reach the runner. It recognises only JS/TS and Python test files. In other languages it runs the full suite with `--cmd` as given (`go test ./...`, `cargo test`).
- **`--all`:** runs `--cmd` as given. Pass it when the runner can't take file paths.

Read each mutant's outcome:

- **SURVIVED:** a test gap. The commit's tests reach that code, but none of them catches the bug.
- **NOT APPLIED:** its `find` didn't match exactly once. Fix it and rerun those mutants once.
- **TIMEOUT:** counts as caught.
- **Exit code 1:** the normal result when any mutant survived or wasn't applied.
- **Exit code 2:** the tests fail before any mutant is applied. Report that instead of test gaps.
- **Exit code 3:** the script itself couldn't run, and its message says why. Fix that and rerun once; if it still fails, write "mutation testing did not run" and the reason.

## 5. Collect /code-review

If the `/code-review` findings arrived after step 3, prove them the same way, with a second repro script.

If step 1 returned only a task id and they haven't arrived yet, run `sleep 20` in the background and check again, up to 4 times. If they still haven't come, write "/code-review did not return".

Once you have the findings, or have given up on them, stop any background `sleep` that is still running (TaskStop, with its task id). Your run then ends when your report does.

## Report

Open with finding 1, and stay under about 40 lines.

1. **Confirmed issues:** bugs the code has now, most severe first, at most 6, merged with the `/code-review` findings. Code that behaves correctly but that no test checks is not a bug: it goes under "Test gaps", and if the code has no bugs, this section says "None".
   - **Tags:** every finding is tagged `both`, `adversarial` or `code-review`.
   - **Each finding:** a one-line summary, the repro (input → wrong output) with the path of the script that ran it, and the fix in one line. A finding without a repro goes under "disproved or unproven" instead.
2. **Test gaps:** each surviving mutant, and the test that should have caught it.
3. **Disproved or unproven:** one line each.
4. **Dismissed:** any finding, yours or `/code-review`'s, that matches an agreed decision, one line each.
