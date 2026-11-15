# Confidential-content publishing

How agents publish PR text, and scan issue text, through the confidential
content guard. Changing the banned terms or the key is in
[confidential-vocabulary.md](confidential-vocabulary.md). Queueing a PR,
including one labeled `confidential-review`, is in
[merge-queue.md](merge-queue.md).

Every guard command is a subcommand of
`python3 .github/scripts/confidential_content_guard.py --repo .`; `--help` on
each lists its options.

## Publish PR text

Publish every agent-authored PR creation, title or body edit, comment, and
review with `publish-pr`. It reads draft files, scans them against the
vocabulary, and passes those same bytes to `gh` through stdin, so the text
that was scanned is the text that is posted. A missing key, a matched term, or
a failed scan stops the GitHub write. The vocabulary cannot match every
sensitive fact: read drafts, links, and attachments yourself as well.

```sh
python3 .github/scripts/confidential_content_guard.py --repo . publish-pr create \
  --base main --head my-branch --title-file /tmp/pr-title.txt \
  --body-file /tmp/pr-body.md
python3 .github/scripts/confidential_content_guard.py --repo . publish-pr edit \
  --pr 123 --body-file /tmp/pr-body.md
python3 .github/scripts/confidential_content_guard.py --repo . publish-pr comment \
  --pr 123 --body-file /tmp/pr-comment.md
python3 .github/scripts/confidential_content_guard.py --repo . publish-pr review \
  --pr 123 --verdict comment --body-file /tmp/pr-review.md
```

- `create` adds `--draft` for a draft PR.
- `edit` takes `--title-file`, `--body-file`, or both.
- `comment --edit-last` edits your last comment instead of adding one.
- `review --verdict` is `approve`, `comment`, or `request-changes`.

After a successful write, `publish-pr` prints `GitHub reference: <url>`.
Record it: it is how you later tell your own write from an identical one.

## Scan issue text

`publish-pr` does not post issues. Scan the draft with
`pr-text --file <draft>` (or `pr-text --stdin`), then submit that same file
with `gh`.
