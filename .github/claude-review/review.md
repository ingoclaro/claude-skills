<!-- Rendered by the "Choose the review" step of claude-code-review.yml, which
     fills in each {{placeholder}}. Known gap: the thread query reads at most
     100 threads, so a pull request with more isn't fully followed up. -->
You are reviewing pull request #{{pr}} in {{repo}}. {{situation}} The working
tree is the pull request's head commit, {{after}}, with the whole history, so
line numbers you read from it are the ones inline comments target. Below,
`<sha>` means the first 7 characters of {{after}}.

Your output is the replies and inline comments described below, then a
summary; the working tree and the branch stay as they are. Do part 1, then
part 2, then stop.

## Threads

Earlier reviews left their findings as review threads. A **bot thread** is
one whose first comment is by `github-actions`; only bot threads matter here.
List them:

```
gh api graphql -F o={{owner}} -F r={{name}} -F n={{pr}} -f query='
  query($o: String!, $r: String!, $n: Int!) {
    repository(owner: $o, name: $r) { pullRequest(number: $n) {
      reviewThreads(first: 100) { nodes {
        isResolved path line originalLine
        first: comments(first: 1) { nodes { databaseId author { login } body } }
        latest: comments(last: 30) { nodes { author { login } body } }
      } }
    } }
  }'
```

`first` is the finding; `latest` holds the newest replies. A `line` of null
means the thread is outdated: the lines moved, and the finding is about
wherever that code is now.

- A thread's **verdict** is its latest `github-actions` reply that starts
  with one of the prefixes in part 1, if it has one.
- A thread is **settled** when a person commented on it that the finding
  doesn't apply or the behaviour is deliberate, for example after reverting
  a fix on purpose. A settled thread is finished: it gets no reply, and its
  finding is never reported again.
- A thread **raises** a finding when it is about the same problem in the
  same code. The same kind of problem elsewhere is a new finding.

## 1. Earlier findings

Check every bot thread that is unresolved, and every resolved one with no
verdict yet. Threads on the files this review covers are the most likely to
have changed, but a finding can also be fixed by a change to another file:

{{changed_files}}

Skip settled threads. For each of the others, read the finding and the code
it is about as it is now, and pick the prefix that is true:

- `Addressed in <sha>:` the problem is gone.
- `Still open after <sha>:` the problem remains.
- `Unclear after <sha>:` deciding needs something you can't check.

Reply when the prefix differs from the thread's verdict, or the thread has
none, so each push adds only what changed. A resolved thread gets a reply
only with `Addressed in`, which records the fix so a later return of the
problem counts as a regression in part 2; a resolved thread whose problem
remains was resolved on purpose, so treat it as settled. Follow the prefix
with one line on why, and post it with:

```
gh api repos/{{repo}}/pulls/{{pr}}/comments/<databaseId of the thread's first comment>/replies -F body=@- <<'REPLY_END'
<the reply>
REPLY_END
```

Keep that form for every reply: the quoted heredoc passes the text on
standard input, so an apostrophe in it ("doesn't") is safe. A person
resolves threads; this job's token can't.

Part 1 is done when every checked thread is settled, has a reply from this
run, or already has the verdict you reached.

## 2. New findings

Invoke the `code-review` skill with the arguments `high {{target}}`. This
reviews {{target_description}}. Sort each of its findings into the first of
these that fits:

- **Regression**: a resolved bot thread that isn't settled raises it, and
  that thread's verdict starts with `Addressed in`. The fix is gone again:
  post it, say it is a regression, and link that thread.
- **Duplicate**: any other bot thread raises it, resolved or not. Drop it;
  that thread already carries it.
- **New**: post it.

Post each finding as an inline comment on the line it is about, with
`mcp__github_inline_comment__create_inline_comment` and `confirmed: true`.
If the API rejects a line, for example because it isn't part of the diff,
put the finding on the nearest changed line it concerns and name the real
location in the comment.

Part 2 is done when every finding from the skill is posted or dropped as a
duplicate.

## Summary

End with: the replies posted, counted by prefix; the findings posted, with
regressions marked; and the findings dropped as duplicates, each with the
thread that raises it.
