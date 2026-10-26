<!-- Rendered by the "Choose the review" step of claude-code-review.yml, which
     fills in each {{placeholder}}. -->
This is a follow-up review of pull request #{{pr}} in {{repo}}, run because new
commits were pushed. The pull request was reviewed before; its previous head
was {{before}} and its head is now {{after}}. The working tree is checked out at
the pull request's merge commit, with the whole history.

Do the two parts below, in order, then stop. Don't edit any file and don't
push. Post on GitHub only in the ways described here.

## 1. Earlier findings

List the review threads the earlier reviews opened, which are the ones whose
first comment is by `github-actions`:

```
gh api graphql -F o={{owner}} -F r={{name}} -F n={{pr}} -f query='
  query($o: String!, $r: String!, $n: Int!) {
    repository(owner: $o, name: $r) { pullRequest(number: $n) {
      reviewThreads(first: 100) { nodes {
        id isResolved isOutdated
        comments(first: 20) { nodes { databaseId author { login } path line originalLine body } }
      } }
    } }
  }'
```

For each unresolved one, read the finding and the code it is about as it is
now, at {{after}}, and decide whether it still holds. An outdated thread (its
`line` is null) only means the lines moved; the problem may still be there
in other lines, so check it the same way. If a later comment in the thread
already settled it, such as a maintainer saying it's deliberate, leave the
thread alone.

- **Fixed:** reply in the thread with `Addressed in <short sha>: <one line on
  what changed>`, then resolve the thread.
- **Still there:** reply with `Still open after <short sha>: <one line on why>`,
  and leave it unresolved.
- **Can't tell:** reply saying what you'd need to know. Don't guess.

Reply with
`gh api repos/{{repo}}/pulls/{{pr}}/comments/<databaseId of the thread's first comment>/replies -f body=...`,
and resolve with
`gh api graphql -F id=<thread id> -f query='mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }'`.

## 2. New findings

Review only what the new commits changed: invoke the `code-review` skill
with the arguments `high {{before}}..{{after}}`. Then take its findings and drop
any that an open thread from part 1 already covers, so nothing is reported
twice. Post each remaining finding as an inline comment on the line it is
about in the new head, with
`mcp__github_inline_comment__create_inline_comment` and `confirmed: true`.

End with a short summary: how many threads were addressed, still open or
unclear, and how many new findings were posted.
