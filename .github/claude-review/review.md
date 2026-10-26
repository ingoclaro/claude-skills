<!-- Rendered by the "Choose the review" step of claude-code-review.yml, which
     fills in each {{placeholder}}. Known gap: the thread query reads at most
     100 threads, so a pull request with more isn't fully followed up. -->
You are reviewing pull request #{{pr}} in {{repo}}. {{situation}} The working
tree is the pull request's merge commit, with the whole history, and its head
commit is {{after}}.

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
        isResolved
        comments(first: 50) { nodes { databaseId author { login } path line originalLine body } }
      } }
    } }
  }'
```

Only the unresolved ones matter, and of those, only the ones on a file in
this list, the files this review covers:

{{changed_files}}

For each such thread, read its finding and the code it is about as it is
now, and decide which of these is true:

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
gh api repos/{{repo}}/pulls/{{pr}}/comments/<databaseId of the thread's first comment>/replies -f body='...'
```

and start the reply with `Addressed in <short sha>:`, `Still open after
<short sha>:` or `Unclear after <short sha>:`, followed by one line on why.
Don't try to resolve threads: this job's token isn't allowed to, so a
person resolves them.

## 2. New findings

Invoke the `code-review` skill with the arguments `high {{target}}`. This
reviews {{target_description}}.

Take its findings and drop every one that an unresolved thread from part 1
already raises, whether or not that thread is on a file listed above, so
nothing is reported twice. Post each remaining finding as an inline comment
on the line it is about in the head commit, with
`mcp__github_inline_comment__create_inline_comment` and `confirmed: true`.
If the API rejects a line, for example because it isn't part of the diff,
put the finding on the nearest changed line it concerns and name the real
location in the comment.

End with a short summary: replies posted by kind, and new findings posted.
