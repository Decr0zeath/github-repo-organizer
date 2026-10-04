# GitHub Repository Organizer

A local browser app for browsing repositories owned by your GitHub account, organizing them with categories, and keeping private comments. It uses Python's standard library and the GitHub CLI, with no dependencies to install through pip and no build step.

## Requirements

- Python 3.7 or newer. The server uses `ThreadingHTTPServer`, postponed annotations, and `subprocess.run` options introduced in Python 3.7.
- GitHub CLI (`gh`) on PATH, signed in to GitHub.com with `gh auth login`.
- A modern browser with JavaScript and `AbortSignal.timeout` support.
- Git on PATH to use **Clone**.

## Start the app

Download or clone the app and keep `serve.py`, `index.html`, and `inventory.js` together in a writable folder.

On Windows, double-click **Open-GitHub-Projects.cmd**. The launcher tries `python`, then `py -3`, and uses the first interpreter that runs successfully and reports Python 3.7 or newer. You can also run `python serve.py --open` in the app folder.

On macOS or Linux, open a terminal in the app folder and run:

```sh
python3 serve.py --open
```

The app opens at `http://127.0.0.1:8765`. Keep the terminal window open while using it; press Ctrl+C to stop. Opening `index.html` directly only shows startup instructions, with no repositories.

If `github-projects.config.json` is missing, first run reads the signed-in GitHub login, fetches all pages of owned repositories, and creates the config atomically. Comments and categories start empty, every repository starts untagged, and the default view shows all repositories with public repositories first. If the CLI is missing or signed out, install it and run `gh auth login`, then restart.

When a config already exists, startup loads it without requiring the CLI or network access. Refresh and cloning require GitHub access. Refresh refuses to replace the inventory when the CLI is signed in to a different account from the config, and names both accounts in the error.

To change the port, run `python serve.py --port 8766 --open` on Windows or `python3 serve.py --port 8766 --open` on macOS or Linux. Reopening the launcher reuses a compatible server already running for the same folder and port. After updating the app, stop the old server and restart it.

## Features

- **Sort:** use the sort menu or repository, visibility, and last-pushed column headers. Missing push dates stay last.
- **Search:** match repository names, descriptions, and languages. Search combines with category filters and is temporary.
- **Categories:** create categories, assign multiple tags to each repository, and filter by any selected category. **Untagged** appears automatically for empty tag lists. Deleting a category removes its assignments.
- **Comments:** edit notes and save with **Save changes**, Ctrl+S, or Cmd+S. Sort order and selected category filters are saved too.
- **Refresh:** retrieve current repository metadata through your `gh` login. Pending edits are saved first. Stable GitHub IDs preserve notes and tags across renames; notes for repositories no longer returned remain in the config. Failed refreshes keep the saved inventory.
- **Clone:** clone a repository into `repos/<repository-name>` in the app folder. Existing folders are never overwritten. One clone runs at a time; cloning leaves pending edits alone. Git and the GitHub CLI are required.

## Local data and configuration

`github-projects.config.json` in the app folder stores the inventory, comments, categories, and view settings. This file is ignored by git and is never uploaded anywhere by the app. Clones in `repos/` are also ignored by git. GitHub requests only retrieve account/repository data or clone repositories; comments and categories stay local. Back up the config to preserve your notes.

Only schema version 3 is supported; other versions produce an unsupported config version error.

| Field | Meaning |
| --- | --- |
| `schemaVersion` | Must be `3` |
| `account` | GitHub login that owns this inventory |
| `updatedAt` | UTC timestamp of the last config write |
| `lastRefreshedAt` | UTC timestamp of the last successful repository fetch |
| `view.sort` | `public-name`, `private-name`, `pushed-newest`, `pushed-oldest`, `name-asc`, or `name-desc` |
| `view.categories` | Selected category filters; an empty list shows all repositories |
| `comments` | Full repository names mapped to comment text; missing entries display as empty |
| `categories` | Unique custom category names; `Untagged` is reserved |
| `repositoryCategories` | Full repository names mapped to lists of custom categories; every current repository needs a list, possibly empty |
| `repositories` | Repository metadata: stable `id`, `name`, `fullName`, `url`, `description`, `language`, `visibility`, `isFork`, `archived`, `disabled`, and `pushedAt` |

Repository keys use `account/repository-name` and must belong to the configured account. Saves use an atomic file replacement. Revision checks prevent an older browser window from overwriting changes made by another window or editor; preserve unsaved notes and reload if a conflict occurs.

The server binds only to localhost and checks request hosts and origins. To maintain separate inventories for multiple accounts, use separate app folders and switch the CLI login before refreshing each one.

## Tests

Run `python -m unittest discover -s tests` (or `python3 -m unittest discover -s tests`). The tests require Python 3.9 or newer and use temporary directories and mocked GitHub commands.
