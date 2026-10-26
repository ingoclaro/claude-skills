#!/usr/bin/env python3
"""Check that a posted Claude review is backed by the files it was shown.

The code-review plugin posts the same fixed comment ("No issues found.
Checked for bugs and CLAUDE.md compliance.") whether it reviewed the whole
diff or skimmed two files, so the comment can't tell you. The transcript can:
a changed file reached the review if its diff hunk appeared in some tool
output (`gh pr diff`, `git diff`), or the file itself was read (`Read`, or
`cat`/`head`/`sed -n` in Bash). A file name alone, as in
`gh pr view --json files`, doesn't count.

Rule: if the run posted anything, every changed file must have reached the
review, or this exits 1 and names the files it never saw. A run that posted
nothing (the plugin stops early on closed, draft, trivial or already-reviewed
PRs) published no verdict, so it only gets a notice.

Prints counts, model names and file paths only, never transcript text: the
logs of a public repo are public.

Usage: review_coverage.py <execution_file> <changed_files.txt>
"""
import json
import os
import re
import sys

READ_CMD = re.compile(r"\b(cat|head|tail|sed -n|less)\b")


def blocks(messages):
    """Every content block of every message, main agent and subagents alike."""
    for m in messages:
        msg = m.get("message") if isinstance(m, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict):
                yield b


def main():
    messages = json.load(open(sys.argv[1]))
    changed = open(sys.argv[2]).read().split()
    reads, shown, outputs, agents, posted = set(), [], [], [], False
    for b in blocks(messages):
        if b.get("type") == "tool_use":
            name, inp = b.get("name"), b.get("input") or {}
            if name == "Read":
                reads.add(inp.get("file_path", ""))
            elif name == "Agent":
                agents.append(f"{inp.get('model') or 'default'}: {inp.get('description', '')}")
            elif name == "Bash":
                cmd = inp.get("command", "")
                if READ_CMD.search(cmd):
                    shown.append(cmd)
                if "gh pr comment" in cmd:
                    posted = True
            elif name == "mcp__github_inline_comment__create_inline_comment":
                posted = True
        elif b.get("type") == "tool_result":
            c = b.get("content")
            outputs.append(c if isinstance(c, str) else json.dumps(c))
    text = "\n".join(outputs)
    seen = [f for f in changed
            if f"diff --git a/{f} b/{f}" in text
            or any(r.endswith("/" + f) for r in reads)
            or any(f in c for c in shown)]
    missed = [f for f in changed if f not in seen]

    result = next((m for m in reversed(messages) if isinstance(m, dict) and m.get("type") == "result"), {})
    if not posted:
        verdict = "nothing posted"
    elif missed:
        verdict = f"posted, but {len(missed)} of {len(changed)} changed files never reached the review"
    else:
        verdict = f"posted, all {len(changed)} changed files reached the review"
    lines = [
        f"Files reviewed: {len(seen)}/{len(changed)}",
        *(f"Never seen: `{f}`" for f in missed),
        f"Subagents: {len(agents)}" + (f" ({'; '.join(agents)})" if agents else ""),
        f"Models: {', '.join(sorted(result.get('modelUsage') or {}))}",
        f"Permission denials: {len(result.get('permission_denials') or [])}",
    ]
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(f"### Claude review: {verdict}\n\n" + "".join(f"- {l}\n" for l in lines))
    print(verdict, *lines, sep="\n")

    if not posted:
        print("::notice::The review posted nothing (it stops early on closed, draft, trivial or already-reviewed PRs).")
    elif missed:
        print(f"::error::The posted review never saw {len(missed)} of {len(changed)} changed files: {', '.join(missed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
