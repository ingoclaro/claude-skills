<!-- Rendered by the "Choose the review" step of claude-code-review.yml, which
     fills in each {{placeholder}}. Known gap: the thread query reads at most
     100 threads, so a pull request with more isn't fully followed up. -->
You are reviewing pull request #{{pr}} in {{repo}}. {{situation}} The working
tree is the pull request's head commit, {{after}}, with the whole history, so
line numbers you read from it are the ones inline comments target.

Do the two parts below, in order, then stop. Don't edit any file and don't
push. Post on GitHub only in the ways described here.

## 1. Earlier findings

Earlier reviews of this pull request left their findings as review threads,
the ones whose first comment is by `github-actions`. List them:

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

`first` is the finding; `latest` holds the newest replies. In this part,
check every unresolved thread, not only those on a file this review covers:
a finding can be fixed by a change to another file. Threads on these files,
the ones this review covers, are the most likely to have changed:

{{changed_files}}

Keep the resolved threads for part 2. For each unresolved thread, read its
finding and the code it is about as it is now, and decide which of these is
true:

- **Fixed**: the problem is gone.
- **Still there**: the problem remains. A thread whose `line` is null is
  outdated, which only means the lines moved, so check it the same way.
- **Can't tell**: you'd need something you can't check.

Then compare that with the thread's latest reply by `github-actions`, if
there is one. Reply only when your verdict is new or differs from it, so a
push doesn't repeat what the thread already says. Leave a thread alone if a
later comment from a person already settled it, for example by saying the
behaviour is deliberate. Reply with:

```
gh api repos/{{repo}}/pulls/{{pr}}/comments/<databaseId of the thread's first comment>/replies -F body=@- <<'REPLY_END'
<the reply>
REPLY_END
```

Start the reply with `Addressed in <short sha>:`, `Still open after
<short sha>:` or `Unclear after <short sha>:`, followed by one line on why.
Pass the text on standard input with a quoted heredoc, as shown, never
inside quotes on the command line: replies are ordinary sentences, and an
apostrophe ("doesn't") would end a quoted argument.
Don't try to resolve threads: this job's token isn't allowed to, so a
person resolves them.

## 2. New findings

Invoke the `code-review` skill with the arguments `high {{target}}`. This
reviews {{target_description}}.

Take its findings and drop every one that a thread from part 1 already
raises, so nothing is reported twice. A thread raises a finding only if it
is about the same problem in the same code; the same kind of problem
elsewhere, for example in a file the thread isn't about, is a new finding.

That includes resolved threads: a person resolved them, either because the
finding was fixed or because they decided it doesn't apply, and either way
it shouldn't come back. The one exception is a regression: the finding was
fixed, and the problem is back. Count it as one only if the thread's latest
`github-actions` reply says `Addressed in …`, or, in a follow-up review,
these commits changed the very lines whose earlier change fixed it. A person
may resolve a thread without a comment because they decided the finding
doesn't apply, so a later edit near the same code is not enough. Post a
regression as a new finding that says so and links the earlier thread.

Post each remaining finding as an inline comment
on the line it is about in the head commit, with
`mcp__github_inline_comment__create_inline_comment` and `confirmed: true`.
If the API rejects a line, for example because it isn't part of the diff,
put the finding on the nearest changed line it concerns and name the real
location in the comment.

End with a short summary: replies posted by kind, and new findings posted.
