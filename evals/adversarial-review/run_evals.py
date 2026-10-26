#!/usr/bin/env python3
"""Run the adversarial-review evals.

Each run is a headless `claude -p` session inside a fresh copy of the eval's
fixture repo. Agent-tool subagents can't be the executor: the skill dispatches
a background reviewer of its own (Sonnet), which calls /code-review. Headless mode
waits for that background agent, so one run covers the whole flow, from
pinning the commit to presenting the report.

Configs:
  with_skill     the skill installed at ~/.claude/skills/adversarial-review,
                 invoked by the eval's `prompt` (the slash command)
  without_skill  the Skill tool disallowed, sent `baseline_prompt` (the same
                 request in plain words)
  opus_reviewer  the skill with its one hard-coded model choice, the
                 reviewer's `model: "sonnet"`, changed to "opus". Built fresh
                 from the current skill at run time (one checked
                 substitution), so it can't drift into a fork.

with_skill refuses to run unless ~/.claude/skills/adversarial-review resolves
to this repo's skills/adversarial-review, so what's measured is the skill in
this checkout. opus_reviewer points that symlink at its variant for its own
phase and restores it on exit (phases run one after another, since the link
is global); it refuses if the install is a real directory rather than a link.

Build the fixtures first (they are generated, not checked in):

    ./setup_fixtures.sh

Each run gets its own TMPDIR under its run dir, passed with --add-dir, because
sandboxes often block /tmp and the reviewer and mutate.ts both write there.
Permissions are an allowlist a user of the skill would plausibly have, not
bypassPermissions: background agents can't prompt, so a denial is part of what
the eval measures. grade_runs.py lists every denial in its diagnostics.

Output layout (iteration dir normally evals/adversarial-review/files/iteration-N, gitignored):
  <iter>/eval-<id>-<name>/<config>/run-<M>/
      repo/              the run's cwd (fixture copy)
      tmp/               the run's TMPDIR: the reviewer's repro scripts land here
      transcript.jsonl   raw `--output-format stream-json --verbose` events,
                         subagents included
      final_message.txt / timing.json / stderr.log

Usage: python3 run_evals.py <iteration-dir> [--runs 3] [--concurrency 2] [--only 1,3]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent          # evals/adversarial-review/
REPO = HERE.parent.parent
DEFAULT_MODEL = "claude-sonnet-5-5"             # the session model; the skill picks the reviewer's
SKILL_LINK = Path.home() / ".claude" / "skills" / "adversarial-review"
SKILL_DEV = REPO / "skills" / "adversarial-review"
CONFIGS = ("with_skill", "without_skill", "opus_reviewer")
# config -> (text in SKILL.md, its replacement) for variants of the skill
VARIANTS = {"opus_reviewer": ('`model: "sonnet"`', '`model: "opus"`')}
# What a developer running reviews plausibly allows. Compound commands are
# allowed only when every part matches, so the everyday utilities a probe or a
# hand-rolled mutation pipes through (cp, sed, grep, tail, ...) are here too:
# without them 13 of 15 baseline runs in iteration-1 had commands denied, which
# measured the harness rather than the model. Claude Code's built-in checks
# (heredocs with braces, $VARS, cd plus a write) can't be allowlisted; see
# README.md, "Sandbox and permissions".
ALLOWED = [
    "Bash(git:*)", "Bash(bun:*)", "Bash(mktemp:*)", "Bash(ls:*)", "Bash(cat:*)",
    "Bash(mkdir:*)", "Bash(sleep:*)", "Bash(cp:*)", "Bash(mv:*)", "Bash(sed:*)",
    "Bash(grep:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(tr:*)", "Bash(wc:*)",
    "Bash(sort:*)", "Bash(diff:*)", "Bash(echo:*)", "Bash(printf:*)", "Bash(cd:*)",
    "Bash(node:*)",
    "Read", "Write", "Edit", "Glob", "Grep", "Agent", "Skill", "TaskStop",
]


def tokens(events):
    """Main-thread tokens plus every subagent's, which `result` usage leaves out.

    A run that waits on the background reviewer ends with one result event per
    main-thread turn, so their usage is summed. Subagents (the reviewer, the
    forked /code-review) report a running total in their task_progress events;
    the last one per task is its total.
    """
    main = sum(v for e in events if e.get("type") == "result"
               for v in (e.get("usage") or {}).values() if isinstance(v, (int, float)))
    sub = {}
    for e in events:
        if e.get("subtype") == "task_progress" and (e.get("usage") or {}).get("total_tokens"):
            sub[e["task_id"]] = max(sub.get(e["task_id"], 0), e["usage"]["total_tokens"])
    return {"total_tokens": main + sum(sub.values()), "main_tokens": main, "subagent_tokens": sum(sub.values())}


def run_job(ev, config, m, iter_dir, model):
    label = f"eval-{ev['id']}-{ev['name']}/{config}/run-{m}"
    run_dir = iter_dir / f"eval-{ev['id']}-{ev['name']}" / config / f"run-{m}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    repo, tmp = run_dir / "repo", run_dir / "tmp"
    shutil.copytree(HERE / "fixtures" / ev["fixture"], repo, symlinks=True)
    tmp.mkdir()
    cmd = [
        "claude", "-p", "--model", model, "--output-format", "stream-json", "--verbose",
        "--permission-mode", "acceptEdits", "--add-dir", str(tmp), "--allowedTools", *ALLOWED,
    ]
    if config != "without_skill":
        # the reviewer reads references/ and runs scripts/ from the install,
        # which is outside the run's cwd
        cmd += ["--add-dir", str(SKILL_LINK.resolve()), "--add-dir", str(SKILL_LINK)]
        prompt = ev["prompt"]
    else:
        cmd += ["--disallowedTools", "Skill"]
        prompt = ev["baseline_prompt"]
    env = {**os.environ, "TMPDIR": str(tmp)}
    start = time.time()
    # An infra failure (timeout, missing CLI) must not abort the batch or pass
    # for a graded 0-score run: record it, and grade_runs.py skips the run.
    try:
        proc = subprocess.run(cmd, input=prompt, text=True, capture_output=True, cwd=repo, env=env, timeout=1800)
    except (subprocess.TimeoutExpired, OSError) as e:
        (run_dir / "error.json").write_text(json.dumps({"error": type(e).__name__, "detail": str(e)[:500]}, indent=2))
        return f"{label} ERROR={type(e).__name__}"
    # wall clock: each result event's own duration covers only one turn
    secs = time.time() - start
    (run_dir / "transcript.jsonl").write_text(proc.stdout)
    (run_dir / "stderr.log").write_text(proc.stderr)
    events = []
    for line in proc.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    results = [e for e in events if e.get("type") == "result"]
    # A non-zero exit, or no result event, leaves nothing to grade: scored, it
    # would fail every assertion and read as a skill failure.
    if not results or proc.returncode != 0:
        (run_dir / "error.json").write_text(json.dumps({
            "error": "CLIFailure", "returncode": proc.returncode, "result_events": len(results),
            "detail": (proc.stderr or proc.stdout)[-500:]}, indent=2))
        return f"{label} ERROR=CLIFailure exit={proc.returncode}"
    last = results[-1]
    (run_dir / "final_message.txt").write_text(last.get("result") or "")
    (run_dir / "timing.json").write_text(json.dumps({
        **tokens(events),
        "duration_ms": int(secs * 1000),
        "total_duration_seconds": round(secs, 1),
        "cost_usd": last.get("total_cost_usd"),
        "turns": len(results),
        "result_subtype": last.get("subtype", "missing"),
    }, indent=2))
    return f"{label} exit={proc.returncode} {secs:.0f}s"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("iteration_dir")
    ap.add_argument("--runs", type=int, default=3)
    # Every with_skill run starts a reviewer agent plus a forked /code-review,
    # so keep this low to stay clear of rate limits.
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--only", help="comma-separated eval ids to run (default: all)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--configs", default=",".join(CONFIGS))
    args = ap.parse_args()

    iter_dir = Path(args.iteration_dir).resolve()
    evals = json.loads((HERE / "evals.json").read_text())["evals"]
    if args.only:
        keep = {int(x) for x in args.only.split(",")}
        evals = [e for e in evals if e["id"] in keep]
    configs = args.configs.split(",")
    unknown = [c for c in configs if c not in CONFIGS]
    if unknown:
        sys.exit(f"unknown config(s): {unknown}; known: {list(CONFIGS)}")
    missing = sorted({ev["fixture"] for ev in evals if not (HERE / "fixtures" / ev["fixture"]).exists()})
    if missing:
        sys.exit(f"missing fixture(s): {missing}; run ./setup_fixtures.sh")
    if "with_skill" in configs and SKILL_LINK.resolve() != SKILL_DEV.resolve():
        sys.exit(f"{SKILL_LINK} doesn't resolve to {SKILL_DEV}, so with_skill would measure another copy. "
                 f"Link it: ln -sfn {SKILL_DEV} {SKILL_LINK}")

    for ev in evals:
        d = iter_dir / f"eval-{ev['id']}-{ev['name']}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "eval_metadata.json").write_text(json.dumps({
            "eval_id": ev["id"], "eval_name": ev["name"], "prompt": ev["prompt"],
            "assertions": [a["text"] for a in ev["assertions"]],
        }, indent=2))

    variants = [c for c in configs if c in VARIANTS]
    if variants and not SKILL_LINK.is_symlink():
        sys.exit(f"{SKILL_LINK} is not a symlink; refusing to swap it for {variants}")
    saved = os.readlink(SKILL_LINK) if SKILL_LINK.is_symlink() else None

    def point_link(target):
        SKILL_LINK.unlink()
        os.symlink(target, SKILL_LINK)

    try:
        # one phase per config: a variant swaps the global symlink
        for config in configs:
            if config in VARIANTS:
                old, new = VARIANTS[config]
                variant = iter_dir / f"_skill-{config}"
                shutil.rmtree(variant, ignore_errors=True)
                shutil.copytree(SKILL_DEV, variant)
                md = (variant / "SKILL.md").read_text()
                if md.count(old) != 1:
                    sys.exit(f"{config}: expected {old} exactly once in SKILL.md, found {md.count(old)}")
                (variant / "SKILL.md").write_text(md.replace(old, new))
                point_link(variant)
                print(f"[{config}] skill -> {variant}", flush=True)
            elif saved is not None and config == "with_skill":
                point_link(saved)
            jobs = [(ev, config, m) for ev in evals for m in range(1, args.runs + 1)]
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                for f in [pool.submit(run_job, ev, c, m, iter_dir, args.model) for ev, c, m in jobs]:
                    print(f.result(), flush=True)
    finally:
        if saved is not None and os.readlink(SKILL_LINK) != saved:
            point_link(saved)
            print(f"restored skill link -> {saved}", flush=True)
    print("ALL RUNS COMPLETE")


if __name__ == "__main__":
    sys.exit(main())
