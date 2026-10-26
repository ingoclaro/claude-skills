#!/usr/bin/env python3
"""Check that a posted Claude review is backed by the files it was shown.

The code-review plugin posts the same fixed comment ("No issues found.
Checked for bugs and CLAUDE.md compliance.") whether it reviewed the whole
diff or skimmed two files, so the comment can't tell you. The transcript can.
A changed file reached the review if:

- its diff header appeared in some tool output (`gh pr diff`, `git diff`),
  matched on the new path, so a renamed file counts; or
- the file itself was read: by `Read`, or by `cat`/`head`/`tail`/`sed -n`
  in Bash, with a path argument that is that file, relative (`a.py`,
  `./a.py`) or absolute under the repo root. So `cat data.py` doesn't count
  for `a.py`, and `/repo/docs/README.md` doesn't count for `README.md`. A
  partial read counts: the check is that the file was shown, not how much.

A large `gh pr diff` output is often saved to a file that the review then
reads with `Read`, which prefixes each line with its number and a tab
(`373\tdiff --git a/f b/f`), as `cat -n` does, so a header counts at the start of
a line or after such a prefix. A listing of headers (`grep -n '^diff'
pr.diff` prints `51:diff --git a/f b/f`) doesn't show the file's change, so
it doesn't count.

A file name alone, as in `gh pr view --json files`, doesn't count.

Rule: if the run posted anything (`gh pr comment`, a comment or review made
through `gh api`, or an inline comment) and the call succeeded, every changed
file must have reached the review, or this exits 1 and names the files it
never saw. A run that posted nothing (the plugin stops early on closed, draft,
trivial or already-reviewed PRs) published no verdict, so it only gets a
notice.

Prints counts, model names, file paths and the first line of up to three
distinct Bash errors (token-shaped text redacted), never other transcript
text: the logs of a public repo are public.

Usage: review_coverage.py <execution_file> <changed_files.txt>
  changed_files.txt: one path per line, as `gh api .../pulls/N/files` lists them
  The repo root is $GITHUB_WORKSPACE, or the current directory.
"""
import json
import os
import re
import sys

READ_CMD = re.compile(r"(^|[\s;&|(])(cat|head|tail|sed -n|less)\s")
GH_API_POST = re.compile(
    r"\bgh api\b(?=.*/(comments|reviews)\b)(?=.*(\s-X\s*POST|\s--method\s+POST|\s-[fF]\s|\s--(raw-)?field|\s--input))"
)
INLINE_COMMENT = "mcp__github_inline_comment__create_inline_comment"
# Anything token-shaped is cut from the error lines this prints.
SECRET = re.compile(r"(sk-ant-|gh[pousr]_|github_pat_)[\w-]+|[A-Za-z0-9+/=_-]{32,}")


def blocks(messages):
    """Every content block of every message, main agent and subagents alike."""
    for m in messages:
        msg = m.get("message") if isinstance(m, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict):
                yield b


def names(path, candidate, root):
    """True when `candidate` is the changed file `path`, relative or absolute."""
    candidate = candidate.strip("'\"")
    return candidate in (path, f"./{path}", os.path.join(root, path))


def mentions(path, command, root):
    """True when some whitespace-separated argument of `command` is `path`."""
    return any(names(path, arg, root) for arg in command.split())


def posts(name, inp):
    """True when this tool call publishes something on the PR."""
    if name == INLINE_COMMENT:
        return True
    cmd = inp.get("command", "") if name == "Bash" else ""
    return bool(re.search(r"\bgh pr comment\b", cmd) or GH_API_POST.search(cmd))


def main():
    with open(sys.argv[1]) as fh:
        messages = json.load(fh)
    with open(sys.argv[2]) as fh:
        changed = [line.strip() for line in fh if line.strip()]
    root = os.environ.get("GITHUB_WORKSPACE") or os.getcwd()

    uses, results = {}, {}
    for b in blocks(messages):
        if b.get("type") == "tool_use":
            uses[b.get("id")] = (b.get("name"), b.get("input") or {})
        elif b.get("type") == "tool_result":
            c = b.get("content")
            results[b.get("tool_use_id")] = (c if isinstance(c, str) else json.dumps(c), bool(b.get("is_error")))

    reads, shown, agents, posted, bash_failed, errors = set(), [], [], False, 0, []
    for use_id, (name, inp) in uses.items():
        output, failed = results.get(use_id, ("", True))
        if name == "Agent":
            agents.append(f"{inp.get('model') or 'default'}: {inp.get('description', '')}")
        if failed:  # denied or errored: nothing was shown, nothing was posted
            if name == "Bash":
                bash_failed += 1
                first = SECRET.sub("[redacted]", (output.strip().splitlines() or [""])[0])[:160]
                if first not in errors:
                    errors.append(first)
            continue
        if name == "Read":
            reads.add(inp.get("file_path", ""))
        elif name == "Bash" and READ_CMD.search(inp.get("command", "")):
            shown.append(inp["command"])
        if posts(name, inp):
            posted = True

    text = "\n".join(out for out, failed in results.values() if not failed)
    seen = [f for f in changed
            if re.search(rf'^(\s*\d+\t)?diff --git .* "?b/{re.escape(f)}"?$', text, re.M)
            or any(names(f, r, root) for r in reads)
            or any(mentions(f, c, root) for c in shown)]
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
        # every Bash call failing means the shell itself is broken (for
        # example the env scrub's bwrap sandbox can't start), not the review
        f"Bash calls failed: {bash_failed} of {sum(1 for n, _ in uses.values() if n == 'Bash')}",
        # the first line of each distinct error, token-shaped text redacted,
        # so a broken shell can be diagnosed without the hidden transcript
        *(f"Bash error: `{e}`" for e in errors[:3]),
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
