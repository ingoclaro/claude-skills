#!/usr/bin/env python3
"""Fill in the review prompt's {{placeholders}} from the workflow's environment.

The prompt contains GraphQL queries with $variables of their own, which is
why the placeholders are {{name}} and not $name. A placeholder left unfilled
is an error, not an empty string, so a typo can't quietly reach the model.

Usage: render.py <prompt.md> <changed-files.txt>
  needs GITHUB_REPOSITORY, PR_NUMBER, MODE (full or follow-up), BEFORE, AFTER
"""
import os
import re
import sys

owner, name = os.environ["GITHUB_REPOSITORY"].split("/")
pr, before, after = os.environ["PR_NUMBER"], os.environ.get("BEFORE", ""), os.environ["AFTER"]
with open(sys.argv[2]) as fh:
    changed = [line.strip() for line in fh if line.strip()]

if os.environ["MODE"] == "follow-up":
    situation = (f"It was reviewed before, and new commits were pushed since: its previous head was "
                 f"{before}. This is a follow-up review of just those commits.")
    target = f"{before}..{after}"
    target_description = "only the commits this push added"
else:
    situation = ("This is a full review of the whole pull request. There may be threads from earlier "
                 "reviews, for example if its branch was rebased or brought up to date with the base branch.")
    target = pr
    target_description = "the whole pull request against its base branch"

values = {
    "pr": pr, "repo": f"{owner}/{name}", "owner": owner, "name": name, "after": after,
    "situation": situation, "target": target, "target_description": target_description,
    "changed_files": "\n".join(f"- `{f}`" for f in changed) or "- (none)",
}
with open(sys.argv[1]) as fh:
    text = re.sub(r"<!--.*?-->\n", "", fh.read(), flags=re.S)
unknown = sorted(set(re.findall(r"{{(\w+)}}", text)) - values.keys())
if unknown:
    sys.exit(f"unknown placeholders in {sys.argv[1]}: {', '.join(unknown)}")
print(re.sub(r"{{(\w+)}}", lambda m: values[m.group(1)], text), end="")
