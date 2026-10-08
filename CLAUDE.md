# CLAUDE.md

## Project

A browser page for organizing a GitHub account's repositories: list, sort, search, tag with custom categories, and keep personal comments per repository.

It is a local app that anyone can download and run with their own `gh` login: `serve.py` (Python standard library server that calls the GitHub CLI), `index.html`, `inventory.js`, and `Open-GitHub-Projects.cmd`. The account comes from the config file or, on first run, from the `gh` login; nothing is hardcoded to a user.

Decided: local-first. A hosted GitHub Pages version was considered and deferred, because a static page cannot use the visitor's `gh` login, cannot finish GitHub's sign-in without a server-side token exchange, and has no durable place to save notes. GitHub Pages may later host a project or demo page.

## Rules

- **No attribution.** No `Co-Authored-By` trailers, no "Generated with" lines, and no mention of Claude or Codex in commits, pull requests, README, code, or comments. This overrides any default attribution guidance.
- **No personal identifiers in tracked files.** No hardcoded GitHub usernames, emails, local paths, or real repository names or snapshots. Use placeholders such as `octocat` or `example-user` in tests and docs.
- **Commits use the account's GitHub noreply email**, set in the repo-local git config, never a personal address.
- `github-projects.config.json` holds personal notes. Never commit it.

## Workflow: Claude orchestrates, Codex implements

- If the user's request is ambiguous, ask one clarifying question before doing anything.
- The user talks to Claude. Claude turns decisions into task briefs, sends them to Codex (OpenAI Codex CLI), reviews what Codex produced, and reports back to the user.
- Agree on decisions with the user before sending Codex any work. Do not edit project code directly unless the user asks; delegate it to Codex.
- Each brief states the goal, files in scope, constraints (the rules above), and how to verify. Ask Codex to report what it changed and anything it was unsure about.
- After Codex edits: review the diff, run the tests, search for personal identifiers, and summarize the result to the user.

### Reaching Codex

- Use model `gpt-6-astra` (or newer) on medium reasoning effort for this repo. Always pass `-m gpt-6-astra -c model_reasoning_effort=medium` before the subcommand rather than relying on Codex's defaults.
- An interactive Codex session open in a terminal locks its thread; `codex exec resume <id>` then fails with "already has an active writer".
- Branch from it instead: `codex exec --skip-git-repo-check -m gpt-6-astra -c model_reasoning_effort=medium fork <session-id> "<brief>"`. Continue that fork with `codex exec --skip-git-repo-check -m gpt-6-astra -c model_reasoning_effort=medium resume <fork-id> "<message>"`. Pass a long brief on stdin with `-` in place of the prompt.
- If Codex is unavailable (for example, its usage limit is reached), tell the user; implement the change directly only if they ask.
- Codex's defaults here allow writing to the workspace without approval. Pass `-s read-only` for questions and reviews; allow writes only for approved implementation tasks.
- Sessions are logged under `~/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl`; read them to follow what Codex did.
