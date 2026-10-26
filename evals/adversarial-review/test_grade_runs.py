"""Unit tests for the transcript parsing in grade_runs.py.

The checks grade real runs, so a parsing bug reads as a skill failure (or a
false pass). These feed them hand-built tool calls instead of transcripts.

Usage: python3 -m unittest test_grade_runs
"""
import json
import unittest

import grade_runs as g

RULE = {"name": "threshold", "file": "src/bytes.ts", "find": "value >= 1023.95", "replace": "value >= 1e9"}
CAP = {"name": "cap", "file": "src/bytes.ts", "find": "unit < UNITS.length - 1) {\n    value", "replace": "x"}
OTHER = {"name": "other", "file": "src/bytes.ts", "find": "bytes < 0", "replace": "bytes < 1"}


class FakeRun:
    """The parts of grade_runs.Run the transcript checks read."""

    def __init__(self, *calls):
        # calls: (tool name, input, output)
        self.tools = [(n, i, None) for n, i, _ in calls]
        self.ids = [str(k) for k in range(len(calls))]
        self.outputs = {str(k): o for k, (_, _, o) in enumerate(calls)}

    def bash_runs(self):
        return [(i.get("command", ""), self.outputs[t]) for (n, i, _), t in zip(self.tools, self.ids) if n == "Bash"]


def write(path, mutants):
    return ("Write", {"file_path": path, "content": json.dumps(mutants, indent=2)}, "ok")


def bash(command, output=""):
    return ("Bash", {"command": command}, output)


def heredoc(path, mutants):
    return f"cat > {path} <<'EOF'\n{json.dumps(mutants, indent=2)}\nEOF"


class MutantObjects(unittest.TestCase):
    def names(self, text):
        return sorted(m.get("name") for m in g.mutant_objects(text))

    def test_heredoc_after_a_test_bracket(self):
        self.assertEqual(self.names("[ -d /tmp/m ] || mkdir /tmp/m; " + heredoc("/tmp/m/m.json", [RULE])), ["threshold"])

    def test_every_array_in_a_command(self):
        two = heredoc("a.json", [OTHER]) + "\n" + heredoc("b.json", [RULE])
        self.assertEqual(self.names(two), ["other", "threshold"])

    def test_objects_without_an_array_with_braces_in_find(self):
        edit = ',\n  {"name": "cond", "find": "if (unit > 0 && value >= 1023.95) {", "replace": "x"}\n]'
        self.assertEqual(self.names(edit), ["cond"])


class GapProven(unittest.TestCase):
    def test_rule_survivor(self):
        run = FakeRun(write("/r/m.json", [RULE, CAP]), bash("bun mutate.ts /r/m.json", "SURVIVED    threshold: all tests passed"))
        self.assertTrue(g.gap_proven(run)[0])

    def test_other_survivor_only(self):
        run = FakeRun(write("/r/m.json", [RULE, CAP]), bash("bun mutate.ts /r/m.json", "SURVIVED    cap: all tests passed"))
        self.assertFalse(g.gap_proven(run)[0])

    def test_survivor_kept_after_the_file_is_rewritten_for_a_rerun(self):
        run = FakeRun(
            write("/r/m.json", [RULE]),
            bash("bun mutate.ts /r/m.json", "SURVIVED    threshold: all tests passed"),
            write("/r/m.json", [OTHER]),
            bash("bun mutate.ts /r/m.json", "caught      other: x"),
        )
        self.assertTrue(g.gap_proven(run)[0])

    def test_find_edited_off_the_rule_before_the_run(self):
        edit = ("Edit", {"file_path": "/r/m.json", "old_string": "value >= 1023.95", "new_string": "value /= 1024;"}, "ok")
        run = FakeRun(write("/r/m.json", [RULE]), edit, bash("bun mutate.ts /r/m.json", "SURVIVED    threshold: all tests passed"))
        self.assertFalse(g.gap_proven(run)[0])

    def test_a_name_reused_in_another_file_is_not_the_rule_mutant(self):
        reused = dict(OTHER, name="threshold")
        run = FakeRun(
            write("/r/rules.json", [RULE]),
            bash(heredoc("/r/other.json", [reused])),
            bash("bun mutate.ts /r/other.json", "SURVIVED    threshold: all tests passed"),
        )
        self.assertFalse(g.gap_proven(run)[0])

    def test_rewritten_by_a_heredoc_with_the_redirect_last(self):
        body = json.dumps([RULE], indent=2)
        run = FakeRun(
            write("/r/m.json", [OTHER]),
            bash(f"cat <<'EOF' > /r/m.json\n{body}\nEOF"),
            bash("bun mutate.ts /r/m.json", "SURVIVED    threshold: all tests passed"),
        )
        self.assertTrue(g.gap_proven(run)[0])

    def test_rewritten_by_echo(self):
        run = FakeRun(
            write("/r/m.json", [OTHER]),
            bash(f"echo '{json.dumps([RULE])}' > /r/m.json"),
            bash("bun mutate.ts /r/m.json", "SURVIVED    threshold: all tests passed"),
        )
        self.assertTrue(g.gap_proven(run)[0])

    def test_a_backup_with_the_same_name_keeps_the_original(self):
        run = FakeRun(
            write("/r/m.json", [RULE]),
            bash("cat /r/m.json > /tmp/bak/m.json"),
            bash("bun mutate.ts /r/m.json", "SURVIVED    threshold: all tests passed"),
        )
        self.assertTrue(g.gap_proven(run)[0])

    def test_output_redirected_to_a_same_named_file_keeps_the_mutants(self):
        run = FakeRun(
            write("/r/m.json", [RULE]),
            bash("bun mutate.ts /r/m.json > /tmp/m.json; tail /tmp/m.json", "SURVIVED    threshold: all tests passed"),
        )
        self.assertTrue(g.gap_proven(run)[0])

    def test_a_comparison_in_a_heredoc_body_is_not_a_redirect(self):
        off_rule = dict(RULE, find="bytes < 0")
        run = FakeRun(
            bash(heredoc("/r/m.json", [RULE])),
            write("/r/m.json", [off_rule]),
            bash("./go.sh", "SURVIVED    threshold: all tests passed"),
        )
        self.assertFalse(g.gap_proven(run)[0])

    def test_the_later_of_two_heredocs_to_one_path_wins(self):
        rule, other = json.dumps([RULE]), json.dumps([dict(OTHER, name="threshold")])
        both = f"cat <<'EOF' > /r/m.json\n{rule}\nEOF\ncat > /r/m.json <<'EOF'\n{other}\nEOF"
        run = FakeRun(bash(both), bash("bun mutate.ts /r/m.json", "SURVIVED    threshold: all tests passed"))
        self.assertFalse(g.gap_proven(run)[0])

    def test_arrows_are_not_redirects(self):
        self.assertEqual(g.REDIRECT.findall("x =>\n  m.json; a -> b; v >= 1"), [])
        self.assertEqual(g.REDIRECT.findall("echo hi > /r/m.json 2>&1"), ["/r/m.json"])

    def test_dev_null_is_not_a_mutants_file(self):
        run = FakeRun(
            write("/r/m.json", [RULE]),
            bash("bun test >/dev/null 2>&1; echo ok"),
            bash("./go.sh 2>/dev/null", "SURVIVED    threshold: all tests passed"),
        )
        self.assertTrue(g.gap_proven(run)[0])

    def test_a_comparison_in_a_python_heredoc_is_not_a_redirect(self):
        run = FakeRun(
            write("/r/m.json", [RULE]),
            bash("python3 - <<'EOF'\nif x > 1: pass\nEOF"),
            bash("./go.sh 1:", "SURVIVED    threshold: all tests passed"),
        )
        self.assertTrue(g.gap_proven(run)[0])

    def test_two_files_echoed_on_separate_lines_dont_mix(self):
        off_rule = dict(OTHER, name="threshold")
        both = f"echo '{json.dumps([RULE])}' > a.json\necho '{json.dumps([off_rule])}' > b.json"
        run = FakeRun(bash(both), bash("bun mutate.ts b.json", "SURVIVED    threshold: all tests passed"))
        self.assertFalse(g.gap_proven(run)[0])

    def test_a_file_name_inside_another_is_not_named(self):
        run = FakeRun(
            write("/r/rules.json", [RULE]),
            write("/r/more-rules.json", [dict(OTHER, name="threshold")]),
            bash("bun mutate.ts /r/more-rules.json", "SURVIVED    threshold: all tests passed"),
        )
        self.assertFalse(g.gap_proven(run)[0])

    def test_run_through_a_wrapper_that_never_names_the_script(self):
        run = FakeRun(
            write("/r/m.json", [RULE]),
            bash("sh /r/run-mutants.sh /r/m.json", "Tests changed in HEAD: x.test.ts\nSURVIVED    threshold: all tests passed"),
        )
        self.assertTrue(g.gap_proven(run)[0])

    def test_results_read_from_a_background_run(self):
        run = FakeRun(
            write("/r/m.json", [RULE]),
            bash("bun mutate.ts /r/m.json", "Command running in background with ID: b1"),
            ("BashOutput", {"bash_id": "b1"}, "SURVIVED    threshold: all tests passed"),
        )
        self.assertTrue(g.gap_proven(run)[0])


class ProbeTests(unittest.TestCase):
    def test_probe_after_a_suite_run(self):
        self.assertEqual(g.probe_tests("bun test src/ && bun test /tmp/probe.test.ts"), ["probe.test.ts"])

    def test_probe_past_flags_and_a_prefix(self):
        self.assertEqual(g.probe_tests("timeout 30 bun test --timeout 5000 ./repro.test.ts"), ["repro.test.ts"])

    def test_suite_run_names_no_probe(self):
        self.assertEqual(g.probe_tests("bun test src/"), [])


if __name__ == "__main__":
    unittest.main()
