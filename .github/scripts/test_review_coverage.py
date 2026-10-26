#!/usr/bin/env python3
"""Tests for review_coverage.py, on small synthetic transcripts.

Run: python3 .github/scripts/test_review_coverage.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_coverage.py")


def call(tool_id, name, inp, output="ok", error=False, sub=False):
    """One tool call and its result, as the execution file records them."""
    parent = "agent-1" if sub else None
    return [
        {"type": "assistant", "parent_tool_use_id": parent,
         "message": {"content": [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}]}},
        {"type": "user", "parent_tool_use_id": parent,
         "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": output, "is_error": error}]}},
    ]


def bash(tool_id, command, output="ok", **kw):
    return call(tool_id, "Bash", {"command": command}, output, **kw)


POST = bash("post", "gh pr comment 7 --body '## Code review'")


ROOT = "/repo"


def run(messages, changed):
    with tempfile.TemporaryDirectory() as d:
        exe, files = os.path.join(d, "exec.json"), os.path.join(d, "changed.txt")
        with open(exe, "w") as fh:
            json.dump(messages + [{"type": "result", "modelUsage": {}, "permission_denials": []}], fh)
        with open(files, "w") as fh:
            fh.write("\n".join(changed) + "\n")
        p = subprocess.run([sys.executable, SCRIPT, exe, files], capture_output=True, text=True,
                           env={**{k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"},
                                "GITHUB_WORKSPACE": ROOT})
        return p.returncode, p.stdout


class Coverage(unittest.TestCase):
    def assertSeen(self, messages, changed, n):
        code, out = run(messages + POST, changed)
        self.assertIn(f"Files reviewed: {n}/{len(changed)}", out)
        self.assertEqual(code, 0 if n == len(changed) else 1, out)

    def test_diff_output_covers_every_file_in_it(self):
        diff = "diff --git a/a.py b/a.py\n+x\ndiff --git a/lib/b.ts b/lib/b.ts\n+y\n"
        self.assertSeen(bash("d", "gh pr diff 7", diff, sub=True), ["a.py", "lib/b.ts"], 2)

    def test_a_file_name_alone_does_not_count(self):
        self.assertSeen(bash("v", "gh pr view 7 --json files", '{"files":[{"path":"a.py"}]}'), ["a.py"], 0)

    def test_substring_of_another_path_does_not_count(self):
        # cat data.py must not cover a.py; cat docs/README.md must not cover README.md
        self.assertSeen(bash("c", "cat data.py") + bash("d", "cat docs/README.md"), ["a.py", "README.md"], 0)

    def test_whole_path_argument_counts(self):
        self.assertSeen(bash("c", "cat a.py") + bash("h", "head -40 'lib/b.ts'"), ["a.py", "lib/b.ts"], 2)

    def test_read_counts_by_path_under_the_repo_root(self):
        self.assertSeen(call("r", "Read", {"file_path": "/repo/src/a.py"}), ["src/a.py"], 1)

    def test_absolute_cat_under_the_repo_root_counts(self):
        self.assertSeen(bash("c", "cat /repo/evals/.gitignore"), ["evals/.gitignore"], 1)

    def test_same_name_in_another_directory_does_not_count(self):
        reads = call("r", "Read", {"file_path": "/repo/docs/README.md"}) + bash("c", "cat /repo/docs/README.md")
        self.assertSeen(reads, ["README.md"], 0)

    def test_a_listing_of_diff_headers_does_not_count(self):
        # grep -n '^diff' prints the header with a line number, not the change itself
        self.assertSeen(bash("g", "grep -n '^diff' /tmp/pr.diff", "51:diff --git a/a.py b/a.py\n"), ["a.py"], 0)

    def test_renamed_file_counts_by_its_new_path(self):
        self.assertSeen(bash("d", "gh pr diff 7", "diff --git a/old.py b/new.py\nsimilarity index 90%\n"), ["new.py"], 1)

    def test_file_name_with_spaces(self):
        self.assertSeen(bash("d", "gh pr diff 7", "diff --git a/my file.md b/my file.md\n+x\n"), ["my file.md"], 1)

    def test_failed_read_does_not_count(self):
        self.assertSeen(bash("c", "cat a.py", "permission denied", error=True), ["a.py"], 0)


class Posting(unittest.TestCase):
    full_diff = bash("d", "gh pr diff 7", "diff --git a/a.py b/a.py\n+x\n")

    def verdict(self, messages, changed=("a.py", "b.py")):
        code, out = run(messages, list(changed))
        return code, out.splitlines()[0]

    def test_nothing_posted_is_a_notice_not_a_failure(self):
        self.assertEqual(self.verdict(self.full_diff), (0, "nothing posted"))

    def test_shallow_review_that_posts_fails(self):
        code, line = self.verdict(self.full_diff + POST)
        self.assertEqual(code, 1)
        self.assertIn("1 of 2 changed files never reached", line)

    def test_denied_post_does_not_count_as_posted(self):
        denied = bash("p", "gh pr comment 7 --body x", "This command requires approval", error=True)
        self.assertEqual(self.verdict(self.full_diff + denied), (0, "nothing posted"))

    def test_gh_api_post_counts_as_posted(self):
        api = bash("p", "gh api repos/o/r/issues/7/comments -f body=x")
        self.assertEqual(self.verdict(self.full_diff + api)[0], 1)

    def test_gh_api_read_of_comments_is_not_a_post(self):
        api = bash("p", "gh api repos/o/r/issues/7/comments --jq '.[].body'")
        self.assertEqual(self.verdict(self.full_diff + api), (0, "nothing posted"))

    def test_failed_bash_calls_are_counted(self):
        broken = bash("a", "gh pr view 7", "bwrap: setting up uid map: Permission denied", error=True)
        _, out = run(broken + bash("b", "gh pr diff 7", "diff --git a/a.py b/a.py\n"), ["a.py"])
        self.assertIn("Bash calls failed: 1 of 2", out)
        self.assertIn("Bash error: `bwrap: setting up uid map: Permission denied`", out)

    def test_error_lines_are_redacted(self):
        leak = bash("a", "env", "token=sk-ant-oat01-abcdefghijklmnopqrstuvwxyz0123456789 failed", error=True)
        _, out = run(leak, ["a.py"])
        self.assertNotIn("abcdefghij", out)
        self.assertIn("[redacted]", out)

    def test_inline_comment_counts_as_posted(self):
        inline = call("i", "mcp__github_inline_comment__create_inline_comment", {"path": "a.py"})
        self.assertEqual(self.verdict(self.full_diff + inline)[0], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
