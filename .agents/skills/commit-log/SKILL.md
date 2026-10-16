---
name: commit-log
description: "Write or review English Conventional Commits titles and bodies for TriCycle; prepare and create accurate commits when the user explicitly requests a commit."
---

# TriCycle Commit Messages

Describe the change's purpose, main behavior, and observed validation. Use the actual working tree and project `git log`; do not infer unimplemented behavior from filenames or requests.

## Format

Use Conventional Commits:

```text
<type>(<scope>): <concise English action description>
```

Choose lowercase `feat`, `fix`, `refactor`, `docs`, `test`, `build`, `chore`, or `perf` according to purpose. Use a familiar module scope such as `sampling`, `reaction-db`, `bohr`, `exploration`, or `reactionopt`; omit scope for cross-module changes when appropriate. Keep the title concise and omit a final period. Titles and bodies, including bullets and validation records, are in English.

For substantive commits, leave a blank line after the title and:

1. Explain the problem, purpose, or capability without repeating the title.
2. Summarize important behavior, workflows/interfaces, and preserved constraints in short paragraphs or bullets grouped by purpose, not a file inventory.
3. Record only commands actually run and results observed, for example `Validation: make check; uv run pytest (80 passed).` Omit validation if none ran; never invent results.

A small self-evident fix may need only a title. Do not add signatures, attribution, or other trailers unless requested.

## Preparing a commit

If asked only for a message, provide the complete proposed title/body without committing. When the user explicitly requests a commit, inspect `git status`, staged/unstaged diffs, and relevant untracked files before staging the actual scope. Exclude unexplained temporary outputs, credentials, and unrelated files.

Review the staged diff before committing and ensure the message covers its main changes. Validation claims must reflect this turn's actual commands and observations. Group closely related work by feature in the body. If unrelated changes cannot be confidently assigned to the requested commit, describe the scope and ask.
