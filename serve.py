"""Local GitHub inventory with durable JSON configuration. Standard library only."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen
import webbrowser


ROOT = Path(__file__).resolve().parent
CONFIG_NAME = "github-projects.config.json"
MAX_BODY = 32 * 1024 * 1024
UNTAGGED = "Untagged"
API_VERSION = 6


class ConfigError(Exception):
    pass


class ConfigConflict(ConfigError):
    pass


class GitHubError(Exception):
    pass


class AboutPartialError(GitHubError):
    def __init__(self, message, result):
        super().__init__(message)
        self.result = result


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_homepage(homepage):
    if isinstance(homepage, str) and not re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", homepage) and not re.search(r"\s", homepage) and "." in homepage.split("/", 1)[0]:
        return "https://" + homepage
    return homepage


def validate_about(description, homepage, topics, editing=False):
    if not isinstance(homepage, str) or not isinstance(topics, list) or any(not isinstance(topic, str) for topic in topics):
        raise ConfigError("Website must be text and topics must be a list of strings.")
    if not isinstance(description, str) or (editing and len(description) > 350):
        raise ConfigError("Description must be text of at most 350 characters when editing.")
    if not editing:
        return
    homepage = normalize_homepage(homepage)
    if len(homepage) > 255:
        raise ConfigError("Website must be empty or an HTTP/HTTPS URL of at most 255 characters.")
    if homepage:
        try:
            url = urlsplit(homepage)
            if url.scheme.lower() not in {"http", "https"} or not url.hostname or any(character.isspace() or ord(character) < 32 for character in homepage):
                raise ValueError("Invalid website")
            url.port
        except ValueError as error:
            raise ConfigError("Website must be empty or an HTTP/HTTPS URL of at most 255 characters.") from error
    if not isinstance(topics, list) or len(topics) > 20 or any(not isinstance(topic, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,49}", topic) for topic in topics):
        raise ConfigError("Use at most 20 topics, each 1 to 50 lowercase letters, digits, or hyphens, starting with a letter or digit.")
    if len(topics) != len(set(topics)):
        raise ConfigError("Topics must be unique.")


def validate_config(config):
    if not isinstance(config, dict) or config.get("schemaVersion") != 3:
        raise ConfigError("Unsupported config version. Only schemaVersion 3 is supported.")
    account = config.get("account")
    if not isinstance(account, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", account):
        raise ConfigError("The account must be a GitHub login.")
    repository_pattern = re.escape(account) + r"/[A-Za-z0-9_.-]+"
    view = config.get("view")
    if not isinstance(view, dict) or view.get("sort") not in ("public-name", "private-name", "pushed-newest", "pushed-oldest", "name-asc", "name-desc"):
        raise ConfigError("Choose a supported repository sort order.")
    comments = config.get("comments")
    if not isinstance(comments, dict):
        raise ConfigError("The comments field must be a repository-to-comment object.")
    for name, comment in comments.items():
        if not re.fullmatch(repository_pattern, name):
            raise ConfigError("A comment has an invalid repository name.")
        if not isinstance(comment, str) or len(comment) > 20000:
            raise ConfigError("Each comment must be text of at most 20,000 characters.")
    categories = config.get("categories")
    if not isinstance(categories, list):
        raise ConfigError("Categories must be a list.")
    for category in categories:
        if not isinstance(category, str) or not category.strip() or category != category.strip() or len(category) > 80 or not category.isprintable():
            raise ConfigError("Category names must contain 1 to 80 characters on one line.")
    if len({name.casefold() for name in categories}) != len(categories):
        raise ConfigError("Category names must be unique, ignoring capitalization.")
    if UNTAGGED.casefold() in {name.casefold() for name in categories}:
        raise ConfigError("Untagged is automatic and cannot be a custom category.")
    selected = view.get("categories")
    if not isinstance(selected, list) or any(not isinstance(name, str) for name in selected) or len(selected) != len(set(selected)) or not set(selected) <= set(categories + [UNTAGGED]):
        raise ConfigError("Choose existing categories or Untagged.")
    assignments = config.get("repositoryCategories")
    if not isinstance(assignments, dict):
        raise ConfigError("Repository categories are missing.")
    for name, tags in assignments.items():
        if not re.fullmatch(repository_pattern, name) or not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise ConfigError("Invalid category assignment.")
        if len(tags) != len(set(tags)) or not set(tags) <= set(categories):
            raise ConfigError("Tags must be unique and use only existing custom categories.")
    repositories = config.get("repositories")
    if not isinstance(repositories, list):
        raise ConfigError("The repository inventory is missing.")
    names, ids = set(), set()
    for repo in repositories:
        if not isinstance(repo, dict) or not isinstance(repo.get("fullName"), str) or not re.fullmatch(repository_pattern, repo["fullName"]):
            raise ConfigError("Invalid repository metadata.")
        name, repo_id = repo["fullName"], repo.get("id")
        if name in names or (repo_id is not None and (type(repo_id) is not int or repo_id <= 0 or repo_id in ids)):
            raise ConfigError("Repository names and GitHub IDs must be unique.")
        names.add(name)
        if repo_id is not None:
            ids.add(repo_id)
        if repo.get("url") != "https://github.com/" + name or repo.get("name") != name.split("/", 1)[1]:
            raise ConfigError("A repository link does not match its name.")
        if repo.get("visibility") not in {"public", "private", "internal"}:
            raise ConfigError("Invalid repository visibility.")
        if any(not isinstance(repo.get(field), str) for field in ("description", "language", "pushedAt")):
            raise ConfigError("Invalid repository text fields.")
        validate_about(repo["description"], repo.get("homepage", ""), repo.get("topics", []))
        if any(type(repo.get(field)) is not bool for field in ("isFork", "archived", "disabled")):
            raise ConfigError("Invalid repository status.")
        if name not in assignments:
            raise ConfigError("Every repository must have a category list, which may be empty.")
    return config


def run_gh(arguments, timeout=120, input_data=None):
    executable = shutil.which("gh")
    if not executable:
        raise GitHubError("Install the GitHub CLI (gh), add it to PATH, and run gh auth login.")
    environment = os.environ.copy()
    environment.pop("GH_DEBUG", None)
    environment["GH_PROMPT_DISABLED"] = "1"
    try:
        result = subprocess.run([executable, "api", "--hostname", "github.com", *arguments], input=input_data, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=environment, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired as error:
        raise GitHubError("GitHub took too long to respond. Try refreshing again.") from error
    except OSError as error:
        raise GitHubError("Could not start gh. Check its installation and reopen the launcher.") from error
    if result.returncode:
        raise GitHubError("GitHub could not be reached with your current gh login. Install the GitHub CLI if needed and run gh auth login, then check your connection and retry.")
    return result.stdout


def github_login():
    login = run_gh(["user", "--jq", ".login"], timeout=30).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", login):
        raise GitHubError("Could not read the signed-in account. Install the GitHub CLI and run gh auth login.")
    return login


def fetch_github_repositories(account):
    login = github_login()
    if login.casefold() != account.casefold():
        raise GitHubError(f"gh is signed in as {login}, but the config belongs to {account}. Switch gh to {account} before refreshing.")
    try:
        pages = json.loads(run_gh(["--paginate", "--slurp", "user/repos?affiliation=owner&per_page=100&sort=full_name"]))
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise ValueError("Expected paginated repositories")
        repositories = []
        for page in pages:
            for raw in page:
                if raw["owner"]["login"].casefold() != account.casefold() or raw["html_url"].casefold() != ("https://github.com/" + account + "/" + raw["name"]).casefold():
                    raise ValueError("Unexpected owner")
                repositories.append({
                    "id": raw["id"], "name": raw["name"], "fullName": account + "/" + raw["name"],
                    "url": "https://github.com/" + account + "/" + raw["name"], "description": raw.get("description") or "",
                    "homepage": raw.get("homepage") or "", "topics": raw.get("topics", []), "language": raw.get("language") or "",
                    "visibility": raw.get("visibility") or ("private" if raw["private"] else "public"),
                    "isFork": raw["fork"], "archived": raw["archived"], "disabled": raw.get("disabled", False),
                    "pushedAt": (raw.get("pushed_at") or "")[:10],
                })
        return repositories
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise GitHubError("GitHub returned an unexpected response. The saved inventory has been kept.") from error


def merge_refresh(config, repositories):
    merged = copy.deepcopy(config)
    previous = {repo["id"]: repo for repo in config["repositories"] if repo.get("id") is not None}
    previous_names = {repo["fullName"]: repo for repo in config["repositories"]}
    current_names = {repo["fullName"] for repo in repositories}
    renamed, added = 0, 0
    for repo in repositories:
        name = repo["fullName"]
        old = previous.get(repo.get("id"))
        if old is None and previous_names.get(name, {}).get("id") is None:
            old = previous_names.get(name)
        if old is not None:
            old_name = old["fullName"]
            merged["comments"][name] = config["comments"].get(old_name, "")
            merged["repositoryCategories"][name] = copy.deepcopy(config["repositoryCategories"].get(old_name, []))
            if old_name != name:
                renamed += 1
                if old_name not in current_names:
                    merged["comments"].pop(old_name, None)
                    merged["repositoryCategories"].pop(old_name, None)
        else:
            added += 1
            if name in previous_names:
                merged["comments"][name] = ""
                merged["repositoryCategories"][name] = []
            else:
                merged["comments"].setdefault(name, "")
                merged["repositoryCategories"].setdefault(name, [])
    merged.update({"repositories": repositories, "lastRefreshedAt": utc_now()})
    validate_config(merged)
    return merged, {"total": len(repositories), "added": added, "renamed": renamed}


class ConfigStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / CONFIG_NAME
        self.lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.clone_lock = threading.Lock()

    def initialize(self):
        with self.lock:
            if self.path.exists():
                return self._read()
            account = github_login()
            repositories = fetch_github_repositories(account)
            config = {
                "schemaVersion": 3, "account": account, "updatedAt": utc_now(),
                "view": {"sort": "public-name", "categories": []},
                "comments": {}, "categories": [],
                "repositoryCategories": {repo["fullName"]: [] for repo in repositories},
                "repositories": repositories, "lastRefreshedAt": utc_now(),
            }
            validate_config(config)
            return self._write(config)

    def clone(self, name):
        if not isinstance(name, str):
            raise ConfigError("Choose a repository from the inventory.")
        repo = next((repo for repo in self.read()["config"]["repositories"] if repo["fullName"] == name), None)
        if repo is None:
            raise ConfigError("Choose a repository from the inventory.")
        if not self.clone_lock.acquire(blocking=False):
            raise ConfigConflict("Another repository is being cloned. Wait for it to finish.")
        try:
            parent = self.root / "repos"
            destination = parent / repo["name"]
            if parent.resolve() != parent or destination.resolve().parent != parent or destination.name in {".", ".."}:
                raise ConfigError("The clone destination must stay inside this project's repos folder.")
            if destination.exists():
                raise ConfigConflict(f"The folder already exists: {destination}. It has not been changed.")
            executable = shutil.which("gh")
            if not executable:
                raise GitHubError("GitHub CLI (gh) was not found. Add it to PATH and reopen the launcher.")
            parent.mkdir(exist_ok=True)
            environment = os.environ.copy()
            environment.pop("GH_DEBUG", None)
            environment["GH_PROMPT_DISABLED"] = "1"
            environment["GIT_TERMINAL_PROMPT"] = "0"
            try:
                result = subprocess.run([executable, "repo", "clone", repo["url"], str(destination)],
                                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                                        timeout=300, env=environment,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except subprocess.TimeoutExpired as error:
                raise GitHubError(f"Clone timed out. Check {destination} for a partial clone before retrying.") from error
            if result.returncode:
                raise GitHubError(f"Clone failed. Check your connection, Git installation, and gh auth status. If {destination} was created, inspect it before retrying.")
            return {"repository": name, "path": str(destination), "cloned": True}
        finally:
            self.clone_lock.release()

    def _read(self):
        try:
            raw = self.path.read_bytes()
            if len(raw) > MAX_BODY:
                raise ConfigError("The configuration file exceeds 32 MB.")
            config = json.loads(raw.decode("utf-8-sig"))
            validate_config(config)
            for repo in config["repositories"]:
                repo.setdefault("homepage", "")
                repo.setdefault("topics", [])
            return {"config": config, "revision": hashlib.sha256(raw).hexdigest()}
        except FileNotFoundError as error:
            raise ConfigError(f"{CONFIG_NAME} is missing from the project directory.") from error
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ConfigError(f"{CONFIG_NAME} contains invalid JSON. Fix the file and reload.") from error

    def read(self):
        with self.lock:
            return self._read()

    def save(self, config, revision, from_github=False):
        if not isinstance(config, dict):
            raise ConfigError("Expected a configuration object.")
        if not isinstance(revision, str):
            raise ConfigError("The configuration revision is missing.")
        with self.lock:
            current = self._read()
            if revision != current["revision"]:
                raise ConfigConflict("The config changed in another window or editor. Copy your unsaved comments, then reload before saving.")
            saved = {
                "schemaVersion": 3,
                "account": current["config"]["account"],
                "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "view": copy.deepcopy(config.get("view")),
                "comments": copy.deepcopy(config.get("comments")),
                "categories": copy.deepcopy(config.get("categories")),
                "repositoryCategories": copy.deepcopy(config.get("repositoryCategories")),
                "repositories": copy.deepcopy(config.get("repositories") if from_github else current["config"]["repositories"]),
                "lastRefreshedAt": config.get("lastRefreshedAt") if from_github else current["config"].get("lastRefreshedAt"),
            }
            validate_config(saved)
            return self._write(saved)

    def _write(self, saved):
        payload = (json.dumps(saved, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(payload) > MAX_BODY:
            raise ConfigError("The saved configuration would exceed 32 MB.")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=self.root, prefix=".github-config-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return {"config": saved, "revision": hashlib.sha256(payload).hexdigest()}

    def about(self, name, revision, description, homepage, topics):
        homepage = normalize_homepage(homepage)
        validate_about(description, homepage, topics, editing=True)
        with self.lock:
            current = self._read()
            if not isinstance(revision, str) or revision != current["revision"]:
                raise ConfigConflict("The config changed in another window or editor. Copy your unsaved comments, then reload before saving.")
            config = current["config"]
            if not isinstance(name, str) or not name.startswith(config["account"] + "/"):
                raise ConfigError("Choose a repository owned by the configured account.")
            repo = next((repo for repo in config["repositories"] if repo["fullName"] == name), None)
            if repo is None:
                raise ConfigError("Choose a repository from the inventory.")
            if repo["archived"]:
                raise ConfigError("Archived repositories cannot be edited.")
            changed = False
            failure = None
            part = "description and website"
            try:
                if description != repo["description"] or homepage != repo["homepage"]:
                    raw = json.loads(run_gh(["--method", "PATCH", "repos/" + name, "--input", "-"], input_data=json.dumps({"description": description, "homepage": homepage})))
                    saved_description = raw["description"] if raw["description"] is not None else ""
                    saved_homepage = raw["homepage"] if raw["homepage"] is not None else ""
                    validate_about(saved_description, saved_homepage, repo["topics"])
                    repo.update({"description": saved_description, "homepage": saved_homepage})
                    changed = True
                part = "topics"
                if set(topics) != set(repo["topics"]):
                    raw = json.loads(run_gh(["--method", "PUT", "repos/" + name + "/topics", "--input", "-"], input_data=json.dumps({"names": topics})))
                    validate_about(repo["description"], repo["homepage"], raw["names"])
                    repo["topics"] = raw["names"]
                    changed = True
            except (GitHubError, ConfigError, ValueError, TypeError, KeyError) as error:
                failure = f"Could not save {part}: {error} Check gh permissions and refresh to confirm GitHub's current values."
                if not changed:
                    raise GitHubError(failure) from error
            if changed:
                if self._read()["revision"] != revision:
                    raise ConfigConflict("GitHub was updated, but the config changed in an editor. Local changes were kept. Reload and refresh to sync the About fields.")
                try:
                    current = self._write(config)
                except (OSError, ConfigError) as error:
                    raise GitHubError("GitHub was updated, but the local config could not be saved. Reload and refresh to sync the About fields.") from error
            result = {"repository": repo, "revision": current["revision"]}
            if failure:
                raise AboutPartialError("Description and website were saved to GitHub and the local config; topics failed. " + failure, result)
            return result

    def refresh(self, revision, fetcher=None):
        if not self.refresh_lock.acquire(blocking=False):
            raise ConfigConflict("A GitHub refresh is already running. Try again when it finishes.")
        try:
            current = self.read()
            if revision != current["revision"]:
                raise ConfigConflict("The config changed. Reload the page before refreshing.")
            repositories = (fetcher or fetch_github_repositories)(current["config"]["account"])
            try:
                merged, summary = merge_refresh(current["config"], repositories)
            except (ConfigError, TypeError, KeyError) as error:
                raise GitHubError("GitHub returned invalid repository metadata. The saved inventory has been kept.") from error
            result = self.save(merged, revision, from_github=True)
            result["refresh"] = summary
            return result
        finally:
            self.refresh_lock.release()


def make_handler(root, store):
    root = Path(root).resolve()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, body, content_type="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            port = self.server.server_port
            return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def do_GET(self):
            if not self.valid_host():
                self.reply(403, {"error": "Only local requests are accepted."})
                return
            path = urlsplit(self.path).path
            try:
                if path in {"/", "/index.html"}:
                    self.reply(200, (root / "index.html").read_bytes(), "text/html; charset=utf-8")
                elif path == "/inventory.js":
                    self.reply(200, (root / "inventory.js").read_bytes(), "text/javascript; charset=utf-8")
                elif path == "/api/config":
                    self.reply(200, store.read())
                elif path == "/api/health":
                    self.reply(200, {"app": "github-project-inventory", "directory": str(root), "apiVersion": API_VERSION})
                else:
                    self.reply(404, {"error": "Not found."})
            except ConfigError as error:
                self.reply(422, {"error": str(error)})
            except OSError:
                self.reply(500, {"error": "Could not read the project files. Check that this folder is available."})

        def do_PUT(self):
            port = self.server.server_port
            origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
            if not self.valid_host() or self.headers.get("Origin") not in origins:
                self.reply(403, {"error": "Save requests must come from this local page."})
                return
            if urlsplit(self.path).path != "/api/config":
                self.reply(404, {"error": "Not found."})
                return
            if self.headers.get_content_type() != "application/json":
                self.reply(415, {"error": "Save requests must contain JSON."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > MAX_BODY:
                    self.reply(413, {"error": "The configuration must be between 1 byte and 32 MB."})
                    return
                document = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(document, dict):
                    raise ConfigError("Expected a configuration object.")
                result = store.save(document.get("config"), document.get("revision"))
                self.reply(200, result)
            except ConfigConflict as error:
                self.reply(409, {"error": str(error)})
            except (ConfigError, ValueError, UnicodeError) as error:
                self.reply(400, {"error": str(error)})
            except OSError:
                self.reply(500, {"error": "Could not save the config file. Check folder permissions and available disk space."})

        def do_POST(self):
            port = self.server.server_port
            if not self.valid_host() or self.headers.get("Origin") not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
                self.reply(403, {"error": "Requests must come from this local page."})
                return
            path = urlsplit(self.path).path
            if path not in {"/api/refresh", "/api/clone", "/api/about"}:
                self.reply(404, {"error": "Not found."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.headers.get_content_type() != "application/json" or not 1 <= length <= MAX_BODY:
                    raise ConfigError("Expected a JSON request.")
                document = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(document, dict):
                    raise ConfigError("Expected a request object.")
                if path == "/api/about":
                    result = store.about(document.get("repository"), document.get("revision"), document.get("description"), document.get("homepage"), document.get("topics"))
                else:
                    result = store.clone(document.get("repository")) if path == "/api/clone" else store.refresh(document.get("revision"))
                self.reply(200, result)
            except ConfigConflict as error:
                self.reply(409, {"error": str(error)})
            except AboutPartialError as error:
                self.reply(502, {"error": str(error), **error.result})
            except GitHubError as error:
                self.reply(502, {"error": str(error)})
            except (ConfigError, ValueError, UnicodeError) as error:
                self.reply(400, {"error": str(error)})
            except OSError:
                self.reply(500, {"error": "Could not access the local files or start the command. Check folder permissions and available disk space."})

        def log_message(self, message, *args):
            print(f"{self.log_date_time_string()} {message % args}", flush=True)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the inventory in the default browser")
    args = parser.parse_args()
    address = f"http://127.0.0.1:{args.port}"
    store = ConfigStore(ROOT)
    try:
        store.initialize()
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(ROOT, store))
    except (ConfigError, GitHubError) as error:
        parser.exit(1, f"Cannot open the inventory: {error}\n")
    except OSError as error:
        try:
            with urlopen(address + "/api/health", timeout=2) as response:
                health = json.load(response)
            if health.get("app") == "github-project-inventory" and Path(health.get("directory", "")).resolve() == ROOT:
                if health.get("apiVersion") != API_VERSION:
                    parser.exit(1, "An older version of the app is still running on this port. Stop it (close its window or press Ctrl+C) and start the app again.\n")
                print(f"Inventory is already running: {address}")
                if args.open:
                    webbrowser.open(address)
                return
        except (OSError, ValueError, URLError):
            pass
        parser.exit(1, f"Cannot start the local page: {error}\nTry: python serve.py --port 8766 --open\n")
    print(f"GitHub Projects: {address}", flush=True)
    print(f"Comments and view settings: {store.path}", flush=True)
    print("Keep this window open while editing. Press Ctrl+C to stop.", flush=True)
    if args.open:
        threading.Timer(0.3, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
