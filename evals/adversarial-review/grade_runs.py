#!/usr/bin/env python3
"""Programmatic grader for adversarial-review eval runs.

Walks <iteration-dir>/eval-*/<config>/run-*/ and writes grading.json per run
(expectations use the exact fields text/passed/evidence the viewer needs).

Each assertion in evals.json names a `check` below. The checks read three
things and never ask a model:

  - the review text: every main-thread assistant message in transcript.jsonl
    (the skill presents the reviewer's report there, then asks what to fix, so
    the final message alone would miss it)
  - the transcript's tool calls, subagents included (stream-json tags theirs
    with parent_tool_use_id)
  - the run's repo, compared with the fixture it was copied from

Ground truth is the bug setup_fixtures.sh planted in each fixture, so the
regexes below match ways of naming that one bug, not review quality in general.

grading.json also carries `diagnostics`, which aren't graded: whether the
reviewer was dispatched and on which model, whether /code-review and the
mutation script ran, permission denials, cost. They're what to read first when
a run scores badly, to tell a skill failure from a harness one.

Also writes outputs/review.md per run (the review text), for skill-creator's
eval viewer.

Usage: python3 grade_runs.py <iteration-dir>
"""
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
I = re.I | re.S


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).stdout.strip()


def exp(text, passed, evidence):
    return {"text": text, "passed": bool(passed), "evidence": str(evidence)[:400]}


def snippet(text, m, ctx=80):
    return text[max(0, m.start() - ctx):m.end() + ctx].replace("\n", " ")


def sentence(text, m):
    """The sentence (or list item) around match `m`."""
    start = max(text.rfind(c, 0, m.start()) for c in ".!?\n") + 1
    ends = [i for i in (text.find(c, m.end()) for c in ".!?\n") if i != -1]
    return text[start:min(ends) if ends else len(text)]


def near(text, a, b, dist):
    """First match of `a` with a match of `b` within `dist` chars either side."""
    for m in re.finditer(a, text, I):
        if re.search(b, text[max(0, m.start() - dist):m.end() + dist], I):
            return m
    return None


class Run:
    def __init__(self, run_dir, ev):
        self.dir = run_dir
        self.ev = ev
        self.repo = run_dir / "repo"
        self.fixture = FIXTURES / ev["fixture"]
        self.events = []
        tp = run_dir / "transcript.jsonl"
        for line in (tp.read_text(errors="replace").splitlines() if tp.exists() else []):
            try:
                self.events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        self.result = next((e for e in reversed(self.events) if e.get("type") == "result"), {})
        texts, self.tools, self.outputs, self.ids = [], [], {}, []
        for e in self.events:
            if e.get("type") == "user":
                for block in e.get("message", {}).get("content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        c = block.get("content")
                        c = c if isinstance(c, str) else json.dumps(c)
                        self.outputs[block.get("tool_use_id")] = ("ERROR " if block.get("is_error") else "") + c
            if e.get("type") != "assistant":
                continue
            for block in e.get("message", {}).get("content", []) or []:
                if block.get("type") == "text" and e.get("parent_tool_use_id") is None:
                    texts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    self.tools.append((block.get("name"), block.get("input") or {}, e.get("parent_tool_use_id")))
                    self.ids.append(block.get("id"))
        texts.append(self.result.get("result") or "")
        self.text = "\n\n".join(t for t in texts if t)

    def bash(self):
        return [i.get("command", "") for n, i, _ in self.tools if n == "Bash"]

    def bash_runs(self):
        """(command, its output) for every Bash call, output "" if none came back."""
        return [(i.get("command", ""), self.outputs.get(t, "")) for (n, i, _), t in zip(self.tools, self.ids) if n == "Bash"]

    def agents(self):
        return [(n, i) for n, i, _ in self.tools if n in ("Agent", "Task")]

    def state(self, repo):
        untracked = sorted(git(repo, "ls-files", "-o", "--exclude-standard").splitlines())
        # Content, not the commit sha: a rebuilt fixture gets new shas, and a
        # run graded after a rebuild must still match it.
        return {
            "head": f'{git(repo, "rev-list", "--count", "HEAD")} commits, top "{git(repo, "log", "-1", "--format=%s")}"'
                    f' tree {git(repo, "rev-parse", "HEAD^{tree}")[:7]}',
            "status": git(repo, "status", "--porcelain"),
            "diff": git(repo, "diff", "HEAD"),
            "untracked": {f: (repo / f).read_text(errors="replace") for f in untracked},
        }

    def unchanged(self):
        got, want = self.state(self.repo), self.state(self.fixture)
        diffs = [k for k in want if got[k] != want[k]]
        return not diffs, (f"differs in {', '.join(diffs)}; status now: {got['status'] or 'clean'}"
                           if diffs else f"HEAD {want['head']}, status {want['status'] or 'clean'}, as in the fixture")


# --- checks: each returns (passed, evidence) --------------------------------

def found(run, pattern, what):
    m = re.search(pattern, run.text, I)
    return (True, snippet(run.text, m)) if m else (False, f"no mention of {what} in the review text")


def reports_floor_bug(run):
    return found(run, r"Math\.floor|\bfloor\b|\bceil|rounds? down|partial (last )?page|remainder|truncat"
                      r"|last page (is )?(unreachable|lost|dropped|missing|never)", "the rounded-down page count")


def reports_empty_bug(run):
    m = near(run.text, r"empty (list|array|input|items|collection)|paginate\(\s*\[\s*\]",
             r"throw|RangeError|out of range|no (valid )?page|0 pages|zero pages|totalPages\W{0,5}(is |=|:)?\s*0", 250)
    return (True, snippet(run.text, m)) if m else (False, "no empty-list failure in the review text")


def reports_validation_bug(run):
    return found(run, r"\b5x\b|1\.5h|\babc\b|garbage|trailing|junk|unrecognized|unknown unit|malformed"
                      r"|invalid (input|unit|string|format|strings)|(never|doesn'?t|does not|won'?t|fails to) throw"
                      r"|silently|returns? 0\b|whole (string|input)|anchor", "accepting malformed input")


def agreed_decision_not_reported(run):
    # Phrases about the decision itself. "No unit" or "90000" alone also turn
    # up in real findings ("1h30" drops its trailing number, "x90" gives 90000),
    # so they don't count as raising it.
    mention = (r"bare (`?\"?\d+\"?`? )?(number|numeric|integer|digit)s?|plain (number|integer)s?|unitless (number|input)"
               r"|(means|meaning|treated as|interpreted as|as) seconds|seconds,? not milliseconds")
    ack = (r"dismiss|agreed|intentional|deliberate|by design|as (you )?(decided|noted|said|specified|stated)"
           r"|(per|your|the) (agreed )?(spec|decision)|not a bug|matches the (spec|decision)|out of scope|excluded"
           r"|as requested|left .{0,40}alone|works as intended")
    # A mention only counts against the run if it criticises the decision:
    # "whitespace around a bare number gives 0" is a different, real bug.
    against = (r"\bshould\b|\bwrong\b|\bbug\b|inconsisten|surpris|confus|ambiguous|footgun|error-prone"
               r"|\brisk|unexpected|counter-?intuitive|(expect|assume)s? milli")
    bad = [m for m in re.finditer(mention, run.text, I)
           if re.search(against, sentence(run.text, m), I)
           and not re.search(ack, run.text[max(0, m.start() - 300):m.end() + 200], I)]
    if bad:
        return False, f"raised without being dismissed: {snippet(run.text, bad[0])}"
    n = len(re.findall(mention, run.text, I))
    return True, f"{n} mention(s), none criticising the decision unacknowledged" if n else "not raised"


def bug_demonstrated(run):
    runner = re.compile(r"\b(bun|node|deno|tsx)\s+(-e|--eval|-p|--print)\b"
                        r"|\b(bun|node|deno|tsx)\s+(run\s+)?\S+\.(m?[jt]sx?)\b"
                        r"|\bnpx\s+tsx\b")
    failed = re.compile(r"^ERROR |No such file|ENOENT|Module not found|not found|Cannot find|permission", re.I | re.M)
    fixture_tests = set(git(run.fixture, "ls-files", "*.test.*").splitlines())
    fixture_tests |= {f.rsplit("/", 1)[-1] for f in fixture_tests}

    def ran(c, out):
        # bun's own errors are lowercase "error:" lines; a repro printing a
        # thrown RangeError is output, not a failure, hence case-sensitive
        # "error: expect(" is a probe test failing as intended: that's the demonstration
        if not out or "mutate.ts" in c or failed.search(out) or re.search(r"^error: (?!expect\()|ModuleNotFound|EACCES", out, re.M):
            return False
        probes = probe_tests(c)
        if probes:  # the output must come from the probe, not a suite run in the same command
            return any(p in out for p in probes)
        direct = [m for m in runner.finditer(c) if not m.group(0).startswith(("bun test", "bun run test"))]
        if direct:
            return True
        if re.search(r"\bbun\s+test\b", c):
            # a suite run counts only if it picked up a test file the fixture
            # doesn't have: a probe written into the repo, run, then removed
            return bool(set(re.findall(r"[\w./-]+\.test\.[jt]sx?", out)) - fixture_tests)
        return False
    hits = [c for c, out in run.bash_runs() if ran(c, out)]
    return (True, hits[0][:200]) if hits else (False, f"{len(run.bash())} Bash calls, none ran a repro that produced output")


def probe_tests(command):
    """File names of the probes a command runs through the test runner.

    A probe is a `bun test` path argument in a temp dir or named probe/repro,
    from any `bun test` in the command, past any prefix (`timeout 30`) or flags. A suite run (`bun test
    src/`) names none.
    """
    names = []
    for seg in re.split(r"&&|\|\||[;|\n]", command):
        m = re.search(r"\bbun\s+test\b(.*)", seg)
        if m:
            names += [t.strip("'\"").rsplit("/", 1)[-1] for t in m.group(1).split()
                      if not t.startswith("-") and re.search(r"/tmp|tmp/|probe|repro", t)]
    return [n for n in names if n]


def reports_cache_aliasing(run):
    # The bug only exists where the cached array meets a caller that mutates it,
    # so a finding has to connect the two, not just say "the cache is unbounded".
    m = (near(run.text, r"effectiveFlags|user\.ts|\bcallers?\b|\.push\(|\bpush(es|ed|ing)?\b",
              r"mutat|shared|alias|same (array|reference|instance)|by reference|leak|accumulat|pollut|corrupt"
              r"|cached array|copy|\[\.\.\.|slice\(\)", 300)
         or re.search(r"(mutat\w*|shared|aliased) (the )?cached (array|value|result)|cached (array|result) is (mutated|shared)"
                      r"|returns? the (same|cached) (array|reference)", run.text, I))
    return (True, snippet(run.text, m)) if m else (False, "no finding links the cached array to a caller mutating it")


ROLLOVER = (r"1023\.95|roll(s|ing)? ?(over|up)|rollover|moves? up|next unit|bump(s|ed)? (up|to)|1048570|1048575"
            r"|1024\.0 (KB|MB|GB)|carr(y|ies)")


def names_rollover_gap(run):
    # The code is correct; the only real gap is that no test reaches the
    # rollover rule. "Tests are thin" or a missing edge elsewhere doesn't count.
    m = near(run.text, ROLLOVER, r"\btests?\b|mutant|surviv|uncovered|untested|cover|gap|asserts?", 250)
    return (True, snippet(run.text, m)) if m else (False, "the rollover rule is never tied to missing tests")


# A mutant's "find" in the rollover rule: its condition, or its block, which
# unlike the loop's `unit++` is followed by the return.
RULE_FIND = r"1023\.95|unit > 0|unit\+\+;\s*\}\s*return"


def mutant_objects(text):
    """The mutant objects in a mutants file, or in a command that writes one."""
    # The whole text, then the span from the first "[" to the last "]": a
    # heredoc or echo around the JSON.
    for cand in (text, *re.findall(r"\[[\s\S]*\]", text)):
        try:
            objs = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        return [o for o in (objs if isinstance(objs, list) else [objs]) if isinstance(o, dict)]
    # Not valid JSON as a whole: its flat objects.
    objs = []
    for m in re.finditer(r"\{[^{}]*\}", text):
        try:
            objs.append(json.loads(m.group(0)))
        except json.JSONDecodeError:
            pass
    return objs


def rule_mutants(run):
    """Names of the mutants written for mutate.ts whose "find" is in the rollover rule.

    Mutants files are followed through Write and Edit, so an edited file is
    read as it stands after the edit; Bash commands are read as written.
    """
    files, texts = {}, []
    for n, i, _ in run.tools:
        if n == "Write":
            files[i.get("file_path")] = i.get("content", "")
            texts.append(files[i.get("file_path")])
        elif n == "Edit":
            path = i.get("file_path")
            old, new = i.get("old_string", ""), i.get("new_string", "")
            if path in files:
                files[path] = files[path].replace(old, new, -1 if i.get("replace_all") else 1)
                texts.append(files[path])
            else:
                texts.append(new)
        elif n == "Bash":
            texts.append(i.get("command", ""))
    return {m["name"] for t in texts for m in mutant_objects(t)
            if isinstance(m.get("name"), str) and re.search(RULE_FIND, str(m.get("find", "")))}


def gap_proven(run):
    # A mutant on the rollover rule (its threshold or its block) that the
    # commit's tests let through: the SURVIVED line must name that mutant, not
    # some other survivor. In the baseline that means doing the mutation by
    # hand: editing the rule and running the suite green.
    rule = rule_mutants(run)
    for c, o in run.bash_runs():
        for line in o.splitlines():
            m = re.match(r"SURVIVED\s+(.*)", line)
            if m and any(m.group(1).startswith(f"{name}:") for name in rule):
                return True, line[:200]
    # By hand: an Edit that takes the rule out of the source, then the next
    # test run still green.
    edited = False
    for (n, i, _), t in zip(run.tools, run.ids):
        if n == "Edit" and "1023.95" in i.get("old_string", "") and "1023.95" not in i.get("new_string", ""):
            edited = True
        elif edited and n == "Bash" and re.search(r"\bbun\s+test\b", i.get("command", "")):
            out = run.outputs.get(t, "")
            if out.startswith("ERROR"):  # denied or failed to start: no test run happened
                continue
            if re.search(r"\b0 fail\b", out):
                return True, f"rule edited out by hand, then: {i['command'][:80]} -> {' '.join(out.split())[:80]}"
            edited = False
    return False, ("no mutant on the rollover rule survived" if rule else "no mutant or edit touched the rollover rule")


def reports_test_gap(run):
    return found(run, r"test gaps?|surviv|not (covered|tested|exercised)|no test|untested|missing tests?"
                      r"|(doesn'?t|does not|never) (test|cover|exercise)|coverage gap"
                      r"|tests? (only|never) (check|cover|exercise|test)", "a test gap")


def repo_untouched(run):
    return run.unchanged()


def asks_commit_or_stash(run):
    return found(run, r"\bstash\b|commit or stash|commit (them|these|it|the changes|your changes|first)"
                      r"|(please|can you|could you) commit", "a commit-or-stash request")


def no_reviewer_dispatched(run):
    a = run.agents()
    return (not a), (f"{len(a)} subagent(s) started: {a[0][1].get('description', '')}" if a else "no subagent started")


def user_changes_preserved(run):
    return run.unchanged()


CHECKS = {f.__name__: f for f in (reports_floor_bug, reports_cache_aliasing, names_rollover_gap, gap_proven, reports_empty_bug, reports_validation_bug,
                                  agreed_decision_not_reported, bug_demonstrated, reports_test_gap,
                                  repo_untouched, asks_commit_or_stash, no_reviewer_dispatched,
                                  user_changes_preserved)}


def diagnostics(run):
    agents = run.agents()
    skills = [(i.get("skill"), p is not None) for n, i, p in run.tools if n == "Skill"]
    mut = [c for c in run.bash() if "mutate.ts" in c]
    return {
        "reviewer_dispatched": [{"model": i.get("model"), "background": i.get("run_in_background"),
                                 "subagent_type": i.get("subagent_type")} for _, i in agents],
        "skills_called": [{"skill": s, "from_subagent": sub} for s, sub in skills],
        "code_review_note": bool(re.search(r"code-review (did not return|unavailable)|did not return", run.text, I)),
        "mutate_runs": len(mut),
        "source_tags": len(re.findall(r"`(both|adversarial|code-review)`|\[(both|adversarial|code-review)\]", run.text, I)),
        "bash_calls": len(run.bash()),
        # Background agents can't prompt, so their denials show up only as
        # system events; the result's own list misses them.
        "permission_denials": [{"tool": e.get("tool_name"), "agent": e.get("agent_id") or "main"}
                               for e in run.events if e.get("subtype") == "permission_denied"],
        # nono's claude profile makes /tmp write-only: a probe written there
        # then fails to load. A run hit by this was blocked, not wrong.
        "tmp_read_errors": sum(1 for o in run.outputs.values()
                               if "/tmp" in o and re.search(r"EACCES|Module not found|ModuleNotFound|Cannot read directory", o)),
        "num_turns": run.result.get("num_turns"),
        "cost_usd": run.result.get("total_cost_usd"),
        "result_subtype": run.result.get("subtype", "missing"),
    }


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    iter_dir = Path(sys.argv[1]).resolve()
    evals = {e["name"]: e for e in json.loads((HERE / "evals.json").read_text())["evals"]}
    graded = skipped = 0
    for run_dir in sorted(iter_dir.glob("eval-*/*/run-*")):
        name = run_dir.parent.parent.name.split("-", 2)[2]
        ev = evals.get(name)
        if ev is None or (run_dir / "error.json").exists() or not (run_dir / "transcript.jsonl").exists():
            print(f"skip {run_dir.relative_to(iter_dir)} (no transcript, an infra error, or an unknown eval)")
            skipped += 1
            continue
        run = Run(run_dir, ev)
        # what the eval viewer shows for the run: the review as the user saw it
        (run_dir / "outputs").mkdir(exist_ok=True)
        (run_dir / "outputs" / "review.md").write_text(run.text)
        expectations = [exp(a["text"], *CHECKS[a["check"]](run)) for a in ev["assertions"]]
        passed = sum(e["passed"] for e in expectations)
        (run_dir / "grading.json").write_text(json.dumps({
            "expectations": expectations,
            "summary": {"passed": passed, "failed": len(expectations) - passed, "total": len(expectations),
                        "pass_rate": round(passed / len(expectations), 3)},
            "diagnostics": diagnostics(run),
        }, indent=2))
        print(f"{run_dir.relative_to(iter_dir)}: {passed}/{len(expectations)}")
        graded += 1
    print(f"graded {graded}, skipped {skipped}")


if __name__ == "__main__":
    main()
