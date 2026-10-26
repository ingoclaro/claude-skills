#!/usr/bin/env python3
"""Fill in a review prompt's {{placeholders}} from the workflow's environment.

The prompts contain GraphQL queries with $variables of their own, which is
why the placeholders are {{name}} and not $name. A placeholder left unfilled
is an error, not an empty string, so a typo can't quietly reach the model.

Usage: render.py <prompt.md>   (needs GITHUB_REPOSITORY, PR_NUMBER, BEFORE, AFTER)
"""
import os
import re
import sys

owner, name = os.environ["GITHUB_REPOSITORY"].split("/")
values = {"pr": os.environ["PR_NUMBER"], "repo": f"{owner}/{name}", "owner": owner, "name": name,
          "before": os.environ["BEFORE"], "after": os.environ["AFTER"]}
with open(sys.argv[1]) as fh:
    text = re.sub(r"<!--.*?-->\n", "", fh.read(), flags=re.S)
unknown = sorted(set(re.findall(r"{{(\w+)}}", text)) - values.keys())
if unknown:
    sys.exit(f"unknown placeholders in {sys.argv[1]}: {', '.join(unknown)}")
print(re.sub(r"{{(\w+)}}", lambda m: values[m.group(1)], text), end="")
