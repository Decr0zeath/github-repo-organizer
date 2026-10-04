import copy
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import serve


ROOT = Path(__file__).resolve().parents[1]


def repository(repo_id, name, visibility="public"):
    return {"id": repo_id, "name": name, "fullName": "octocat/" + name,
            "url": "https://github.com/octocat/" + name, "description": "A project",
            "language": "Python", "visibility": visibility, "isFork": False,
            "archived": False, "disabled": False, "pushedAt": "2024-01-01"}


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=ROOT, prefix=".inventory-test-")
        self.root = Path(self.directory.name).resolve()
        self.assertTrue(self.root.is_relative_to(ROOT))
        self.addCleanup(self.directory.cleanup)
        self.repos = [repository(1, "project"), repository(2, "research-example", "private")]
        self.config = {"schemaVersion": 3, "account": "octocat", "updatedAt": None,
                       "view": {"sort": "public-name", "categories": []},
                       "categories": [], "repositories": self.repos,
                       "lastRefreshedAt": "2024-01-01T00:00:00Z",
                       "comments": {repo["fullName"]: "" for repo in self.repos},
                       "repositoryCategories": {repo["fullName"]: [] for repo in self.repos}}
        self.config["comments"]["octocat/project"] = "Keep this note\nUnicode: \u4e2d\u6587"
        self.path = self.root / serve.CONFIG_NAME
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        self.store = serve.ConfigStore(self.root)

    def save(self, config):
        return self.store.save(config, self.store.read()["revision"])

    def test_first_run_creates_config_from_github(self):
        self.path.unlink()
        pages = [[{"id": repo["id"], "name": repo["name"], "owner": {"login": "octocat"},
                   "html_url": repo["url"], "description": repo["description"], "language": repo["language"],
                   "visibility": repo["visibility"], "private": repo["visibility"] == "private",
                   "fork": False, "archived": False, "disabled": False, "pushed_at": repo["pushedAt"]}]
                 for repo in self.repos]
        with patch.object(serve, "run_gh", side_effect=["octocat\n", "octocat\n", json.dumps(pages)]) as cli, patch.object(serve.os, "replace", wraps=serve.os.replace) as replace:
            result = self.store.initialize()
        self.assertEqual(cli.call_args_list[0].args[0], ["user", "--jq", ".login"])
        self.assertIn("--paginate", cli.call_args.args[0])
        replace.assert_called_once()
        saved = result["config"]
        self.assertEqual(saved["schemaVersion"], 3)
        self.assertEqual(saved["account"], "octocat")
        self.assertEqual(saved["repositories"], self.repos)
        self.assertEqual(saved["comments"], {})
        self.assertEqual(saved["categories"], [])
        self.assertEqual(saved["repositoryCategories"], {repo["fullName"]: [] for repo in self.repos})
        self.assertEqual(saved["view"], {"sort": "public-name", "categories": []})
        self.assertRegex(saved["lastRefreshedAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(self.store.read(), result)
        self.assertFalse(list(self.root.glob(".github-config-*.tmp")))

    def test_existing_config_startup_does_not_call_github(self):
        before = self.path.read_bytes()
        with patch.object(serve, "run_gh", side_effect=AssertionError("Unexpected GitHub call")) as cli, patch.object(serve, "ROOT", self.root), patch("sys.argv", ["serve.py"]), patch.object(serve, "ThreadingHTTPServer") as server, patch("builtins.print"):
            serve.main()
        cli.assert_not_called()
        server.return_value.serve_forever.assert_called_once_with()
        self.assertEqual(self.path.read_bytes(), before)

    def test_first_run_missing_cli_or_login_exits_without_config(self):
        self.path.unlink()
        for executable in (None, "gh"):
            with self.subTest(executable=executable), patch.object(serve, "ROOT", self.root), patch("sys.argv", ["serve.py"]), patch.object(serve.shutil, "which", return_value=executable), patch.object(serve.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "not signed in")), patch.object(serve, "ThreadingHTTPServer") as server, patch("sys.stderr") as stderr:
                with self.assertRaises(SystemExit) as error:
                    serve.main()
                self.assertEqual(error.exception.code, 1)
                message = "".join(call.args[0] for call in stderr.write.call_args_list)
                self.assertIn("Install the GitHub CLI", message)
                self.assertIn("gh auth login", message)
                server.assert_not_called()
                self.assertFalse(self.path.exists())

    def test_refresh_rejects_different_login_without_changing_notes(self):
        before = self.path.read_bytes()
        with patch.object(serve, "run_gh", return_value="another-user\n") as cli:
            with self.assertRaisesRegex(serve.GitHubError, "another-user.*octocat"):
                self.store.refresh(self.store.read()["revision"])
        cli.assert_called_once_with(["user", "--jq", ".login"], timeout=30)
        self.assertEqual(self.path.read_bytes(), before)

    def test_arbitrary_account_validation_and_save(self):
        changed = json.loads(json.dumps(self.config).replace("octocat", "another-user"))
        self.assertEqual(serve.validate_config(changed), changed)
        self.path.write_text(json.dumps(changed), encoding="utf-8")
        self.assertEqual(self.save(changed)["config"]["account"], "another-user")
        for field in ("repositories", "comments", "repositoryCategories"):
            invalid = copy.deepcopy(changed)
            invalid[field] = copy.deepcopy(self.config[field])
            with self.subTest(field=field), self.assertRaises(serve.ConfigError):
                serve.validate_config(invalid)

    def test_unsupported_config_versions_are_rejected(self):
        for version in (1, 2, 4, None, "3"):
            changed = copy.deepcopy(self.config)
            changed["schemaVersion"] = version
            self.path.write_text(json.dumps(changed), encoding="utf-8")
            with self.subTest(version=version), self.assertRaisesRegex(serve.ConfigError, "Unsupported config version"):
                self.store.initialize()

    def test_empty_tags_and_multiple_filter_categories_persist(self):
        changed = copy.deepcopy(self.config)
        changed["categories"] = ["Work", "Personal"]
        changed["view"]["categories"] = ["Untagged", "Work"]
        self.save(changed)
        self.assertEqual(self.store.read()["config"]["view"], changed["view"])
        self.assertEqual(self.store.read()["config"]["repositoryCategories"], changed["repositoryCategories"])

    def test_sort_orders_persist_and_unknown_sort_is_rejected(self):
        for order in ("public-name", "private-name", "pushed-newest", "pushed-oldest", "name-asc", "name-desc"):
            changed = copy.deepcopy(self.config)
            changed["view"]["sort"] = order
            self.save(changed)
            self.assertEqual(self.store.read()["config"]["view"]["sort"], order)
        changed["view"]["sort"] = "unknown"
        with self.assertRaises(serve.ConfigError):
            self.save(changed)

    def test_invalid_filters_and_explicit_untagged_are_rejected(self):
        for selection in (["Missing"], ["Untagged", "Untagged"], "Untagged"):
            changed = copy.deepcopy(self.config)
            changed["view"]["categories"] = selection
            with self.assertRaises(serve.ConfigError):
                self.save(changed)
        changed = copy.deepcopy(self.config)
        changed["categories"] = ["Untagged"]
        with self.assertRaises(serve.ConfigError):
            self.save(changed)

    def test_many_custom_categories_and_multiple_tags_persist(self):
        changed = copy.deepcopy(self.config)
        changed["categories"] += [f"Category {index}" for index in range(1001)]
        changed["repositoryCategories"]["octocat/project"] += ["Category 0", "Category 1000"]
        self.save(changed)
        reopened = serve.ConfigStore(self.root).read()["config"]
        self.assertEqual(reopened["categories"], changed["categories"])
        self.assertEqual(reopened["repositoryCategories"], changed["repositoryCategories"])
        self.assertEqual(reopened["comments"], self.config["comments"])

    def test_duplicate_categories_and_undefined_tags_rejected(self):
        changed = copy.deepcopy(self.config)
        changed["categories"] = ["Work", "work"]
        with self.assertRaises(serve.ConfigError):
            self.save(changed)
        changed = copy.deepcopy(self.config)
        changed["repositoryCategories"]["octocat/project"].append("Unknown")
        with self.assertRaises(serve.ConfigError):
            self.save(changed)

    def test_refresh_adds_repositories_and_preserves_notes_and_tags_on_rename(self):
        changed = copy.deepcopy(self.config)
        changed["categories"].append("My work")
        changed["repositoryCategories"]["octocat/project"].append("My work")
        state = self.save(changed)
        incoming = [repository(1, "renamed-project"), self.repos[1], repository(3, "speaker-new")]
        result = self.store.refresh(state["revision"], lambda account: incoming)
        saved = result["config"]
        self.assertEqual(saved["comments"]["octocat/renamed-project"], changed["comments"]["octocat/project"])
        self.assertEqual(saved["repositoryCategories"]["octocat/renamed-project"], ["My work"])
        self.assertEqual(saved["repositoryCategories"]["octocat/speaker-new"], [])
        self.assertEqual(result["refresh"], {"total": 3, "added": 1, "renamed": 1})
        self.assertEqual(serve.ConfigStore(self.root).read()["config"], saved)

    def test_unavailable_repository_keeps_its_notes(self):
        state = self.store.read()
        result = self.store.refresh(state["revision"], lambda account: [self.repos[1]])
        self.assertEqual(result["config"]["comments"]["octocat/project"], self.config["comments"]["octocat/project"])

    def test_failed_refresh_and_invalid_metadata_do_not_replace_config(self):
        before = self.path.read_bytes()
        with self.assertRaises(serve.GitHubError):
            self.store.refresh(self.store.read()["revision"], lambda account: (_ for _ in ()).throw(serve.GitHubError("offline")))
        with self.assertRaises(serve.GitHubError):
            self.store.refresh(self.store.read()["revision"], lambda account: [self.repos[0], self.repos[0]])
        self.assertEqual(self.path.read_bytes(), before)

    def test_refresh_cannot_overwrite_another_windows_save(self):
        state = self.store.read()
        def slow_fetch(account):
            edited = copy.deepcopy(self.config)
            edited["comments"]["octocat/project"] = "Saved in another window"
            self.save(edited)
            return self.repos
        with self.assertRaises(serve.ConfigConflict):
            self.store.refresh(state["revision"], slow_fetch)
        self.assertEqual(self.store.read()["config"]["comments"]["octocat/project"], "Saved in another window")

    def test_write_failure_keeps_original_config(self):
        before = self.path.read_bytes()
        with patch.object(serve.os, "replace", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                self.save(self.config)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.root.glob(".github-config-*.tmp")))

    def test_cli_reads_all_pages_and_checks_account(self):
        pages = []
        for repo in self.repos:
            pages.append([{"id": repo["id"], "name": repo["name"], "owner": {"login": "octocat"},
                           "html_url": repo["url"], "description": repo["description"], "language": repo["language"],
                           "visibility": repo["visibility"], "private": repo["visibility"] == "private", "fork": False,
                           "archived": False, "disabled": False, "pushed_at": "2024-01-01T06:00:00Z"}])
        with patch.object(serve, "run_gh", side_effect=["OCTOCAT\n", json.dumps(pages)]) as cli:
            self.assertEqual(serve.fetch_github_repositories("octocat"), self.repos)
            self.assertIn("--paginate", cli.call_args.args[0])
            self.assertIn("--slurp", cli.call_args.args[0])
        with patch.object(serve, "run_gh", return_value="DifferentAccount"):
            with self.assertRaises(serve.GitHubError):
                serve.fetch_github_repositories("octocat")

    def test_cli_rejects_repositories_owned_by_another_account(self):
        pages = [[{"owner": {"login": "another-user"}}]]
        with patch.object(serve, "run_gh", side_effect=["octocat", json.dumps(pages)]):
            with self.assertRaises(serve.GitHubError):
                serve.fetch_github_repositories("octocat")

    def test_cli_timeout_reports_recoverable_error(self):
        with patch.object(serve.shutil, "which", return_value="gh"), patch.object(serve.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 120)):
            with self.assertRaises(serve.GitHubError):
                serve.run_gh(["user"])

    def test_http_save_and_refresh_use_the_config_file(self):
        for name in ("index.html", "inventory.js"):
            (self.root / name).write_bytes((ROOT / name).read_bytes())
        handler = serve.make_handler(self.root, self.store)
        handler.log_message = lambda *args: None
        server = serve.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        address = f"http://127.0.0.1:{server.server_port}"
        with urlopen(address + "/inventory.js", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("javascript", response.headers["Content-Type"])
        for route in ("/", "/index.html"):
            with urlopen(address + route, timeout=5) as response:
                page = response.read().decode("utf-8")
                self.assertIn("GitHub Repository Organizer", page)
                self.assertNotIn('id="inventory-data"', page)
        changed = copy.deepcopy(self.config)
        changed["categories"].append("Custom")
        changed["repositoryCategories"]["octocat/project"].append("Custom")
        headers = {"Content-Type": "application/json", "Origin": address}
        body = json.dumps({"revision": self.store.read()["revision"], "config": changed}).encode()
        with urlopen(Request(address + "/api/config", data=body, headers=headers, method="PUT"), timeout=5) as response:
            result = json.load(response)
        with patch.object(serve, "fetch_github_repositories", return_value=self.repos + [repository(3, "new")]):
            body = json.dumps({"revision": result["revision"]}).encode()
            with urlopen(Request(address + "/api/refresh", data=body, headers=headers, method="POST"), timeout=5) as response:
                refreshed = json.load(response)
        self.assertEqual(refreshed["config"]["repositoryCategories"]["octocat/project"], ["Custom"])
        self.assertEqual(refreshed["config"], json.loads(self.path.read_text(encoding="utf-8")))
        clone_body = json.dumps({"repository": "octocat/project"}).encode()
        with patch.object(serve.shutil, "which", return_value="gh"), patch.object(serve.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as command:
            with urlopen(Request(address + "/api/clone", data=clone_body, headers=headers, method="POST"), timeout=5) as response:
                cloned = json.load(response)
            self.assertTrue(cloned["cloned"])
            self.assertEqual(command.call_args.args[0][1:3], ["repo", "clone"])
        bad_headers = {"Content-Type": "application/json", "Origin": "https://unrelated.example"}
        with self.assertRaises(HTTPError) as clone_error:
            urlopen(Request(address + "/api/clone", data=clone_body, headers=bad_headers, method="POST"), timeout=5)
        self.assertEqual(clone_error.exception.code, 403)
        clone_error.exception.close()
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(address + "/api/refresh", data=body, headers=bad_headers, method="POST"), timeout=5)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

    def test_clone_uses_inventory_url_and_preserves_config(self):
        before = self.path.read_bytes()
        with patch.object(serve.shutil, "which", return_value="gh"), patch.object(serve.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as command:
            result = self.store.clone("octocat/project")
        self.assertEqual(result["path"], str(self.root / "repos" / "project"))
        self.assertEqual(command.call_args.args[0], ["gh", "repo", "clone", "https://github.com/octocat/project", result["path"]])
        self.assertEqual(command.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(self.path.read_bytes(), before)

    def test_clone_rejects_unknown_names_and_existing_folders(self):
        with patch.object(serve.subprocess, "run") as command:
            for name in (None, "octocat/../escape", "Other/project", "octocat/missing"):
                with self.assertRaises(serve.ConfigError):
                    self.store.clone(name)
            target = self.root / "repos" / "project"
            target.mkdir(parents=True)
            note = target / "local-work.txt"
            note.write_text("keep", encoding="utf-8")
            with self.assertRaises(serve.ConfigConflict):
                self.store.clone("octocat/project")
            self.assertEqual(note.read_text(encoding="utf-8"), "keep")
            command.assert_not_called()

    def test_clone_failure_and_timeout_release_lock(self):
        with patch.object(serve.shutil, "which", return_value="gh"):
            for failure in (subprocess.CompletedProcess([], 1, "", "failed"), subprocess.TimeoutExpired("gh", 300)):
                kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch.object(serve.subprocess, "run", **kwargs), self.assertRaises(serve.GitHubError):
                    self.store.clone("octocat/project")
                self.assertFalse(self.store.clone_lock.locked())
        self.store.clone_lock.acquire()
        try:
            with self.assertRaises(serve.ConfigConflict):
                self.store.clone("octocat/project")
        finally:
            self.store.clone_lock.release()


if __name__ == "__main__":
    unittest.main()
