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
            "homepage": "", "topics": [],
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
        pages[0][0].update({"homepage": "https://example.com/project", "topics": ["python", "local-app"]})
        pages[1][0]["homepage"] = None
        expected = copy.deepcopy(self.repos)
        expected[0].update({"homepage": "https://example.com/project", "topics": ["python", "local-app"]})
        with patch.object(serve, "run_gh", side_effect=["OCTOCAT\n", json.dumps(pages)]) as cli:
            self.assertEqual(serve.fetch_github_repositories("octocat"), expected)
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


class AboutTests(unittest.TestCase):
    setUp = InventoryTests.setUp

    def request_about(self, changes=None, headers=None):
        if not hasattr(self, "address"):
            handler = serve.make_handler(self.root, self.store)
            handler.log_message = lambda *args: None
            server = serve.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            self.address = f"http://127.0.0.1:{server.server_port}"
        document = {"repository": "octocat/project", "revision": self.store.read()["revision"],
                    "description": "New description", "homepage": "https://example.com", "topics": ["python", "local-app"]}
        document.update(changes or {})
        request_headers = {"Content-Type": "application/json", "Origin": self.address}
        request_headers.update(headers or {})
        request = Request(self.address + "/api/about", data=json.dumps(document).encode(), headers=request_headers, method="POST")
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.code, json.load(response)

    def test_old_config_defaults_do_not_rewrite_file(self):
        for repo in self.config["repositories"]:
            repo.pop("homepage")
            repo.pop("topics")
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        before = self.path.read_bytes()
        state = self.store.read()
        for repo in state["config"]["repositories"]:
            self.assertEqual(repo["homepage"], "")
            self.assertEqual(repo["topics"], [])
        self.assertEqual(state["config"]["schemaVersion"], 3)
        self.assertEqual(self.path.read_bytes(), before)

    def test_about_validation_limits(self):
        serve.validate_about("x" * 350, "https://example.com/" + "x" * 235, ["a" * 50], editing=True)
        serve.validate_about("", "", ["topic-" + str(index) for index in range(20)], editing=True)
        invalid = [
            {"description": "x" * 351}, {"description": None},
            {"homepage": "https://example.com/" + "x" * 236}, {"homepage": None},
            {"homepage": "javascript:alert(1)"}, {"homepage": "ftp://example.com"},
            {"homepage": "https://"}, {"homepage": "not-a-host"},
            {"homepage": "https://example.com/\npath"}, {"homepage": "https://[invalid"},
            {"topics": "python"}, {"topics": ["Python"]}, {"topics": ["-python"]},
            {"topics": ["python_test"]}, {"topics": [""]}, {"topics": ["a" * 51]},
            {"topics": ["python", "python"]}, {"topics": [None]},
            {"topics": ["topic-" + str(index) for index in range(21)]},
        ]
        for change in invalid:
            fields = {"description": "", "homepage": "", "topics": []}
            fields.update(change)
            with self.subTest(change=change), self.assertRaises(serve.ConfigError):
                serve.validate_about(**fields, editing=True)
        self.config["repositories"][0]["description"] = "x" * 351
        serve.validate_config(self.config)
        for field, value in (("homepage", None), ("topics", "python"), ("topics", [None])):
            changed = copy.deepcopy(self.config)
            changed["repositories"][0][field] = value
            with self.subTest(field=field), self.assertRaises(serve.ConfigError):
                serve.validate_config(changed)

    def test_stored_about_content_loads_without_edit_validation(self):
        for homepage in ("someone.github.io/project", "javascript:alert(1)", "not a URL", "x" * 300):
            with self.subTest(homepage=homepage):
                self.config["repositories"][0].update({"homepage": homepage, "topics": ["UPPER_case"] * 21 + ["x" * 51]})
                self.path.write_text(json.dumps(self.config), encoding="utf-8")
                self.assertEqual(self.store.read()["config"], self.config)

    def test_refresh_stores_schemeless_homepage_unchanged(self):
        raw = {"id": 1, "name": "project", "owner": {"login": "octocat"},
               "html_url": "https://github.com/octocat/project", "description": "A project",
               "homepage": "someone.github.io/project", "topics": ["UPPER_case"],
               "language": "Python", "private": False, "fork": False, "archived": False,
               "pushed_at": "2024-01-01T00:00:00Z"}
        with patch.object(serve, "run_gh", side_effect=["octocat", json.dumps([[raw]])]):
            result = self.store.refresh(self.store.read()["revision"])
        repo = result["config"]["repositories"][0]
        self.assertEqual(repo["homepage"], raw["homepage"])
        self.assertEqual(repo["topics"], raw["topics"])
        self.assertEqual(self.store.read()["config"], result["config"])

    def test_schemeless_edit_sends_normalized_homepage(self):
        serve.validate_about("", "example.com", [], editing=True)
        with patch.object(serve, "run_gh", return_value=json.dumps({"description": "A project", "homepage": "https://example.com"})) as cli:
            status, result = self.request_about({"description": "A project", "homepage": "example.com", "topics": []})
        self.assertEqual(status, 200)
        self.assertEqual(cli.call_count, 1)
        self.assertEqual(json.loads(cli.call_args.kwargs["input_data"])["homepage"], "https://example.com")
        self.assertEqual(result["repository"]["homepage"], "https://example.com")
        # Compare after normalization: the same input now needs no GitHub update.
        with patch.object(serve, "run_gh") as cli:
            status, _ = self.request_about({"description": "A project", "homepage": "example.com", "topics": []})
        self.assertEqual(status, 200)
        cli.assert_not_called()

    def test_topics_edit_with_stored_schemeless_homepage(self):
        homepage = "someone.github.io/project"
        self.config["repositories"][0]["homepage"] = homepage
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        replies = [json.dumps({"description": "A project", "homepage": homepage}), json.dumps({"names": ["python"]})]
        with patch.object(serve, "run_gh", side_effect=replies) as cli:
            status, result = self.request_about({"description": "A project", "homepage": homepage, "topics": ["python"]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(cli.call_args_list[0].kwargs["input_data"])["homepage"], "https://" + homepage)
        self.assertEqual(cli.call_args_list[1].args[0][1], "PUT")
        self.assertEqual(result["repository"]["topics"], ["python"])
        self.assertEqual(result["repository"]["homepage"], homepage)

    def test_about_http_success_uses_json_stdin_and_response_metadata(self):
        revision = self.store.read()["revision"]
        replies = [json.dumps({"description": "GitHub description", "homepage": "https://example.com/"}),
                   json.dumps({"names": ["local-app", "python"]})]
        with patch.object(serve, "run_gh", side_effect=replies) as cli:
            status, result = self.request_about()
        self.assertEqual(status, 200)
        self.assertEqual(cli.call_args_list[0].args[0], ["--method", "PATCH", "repos/octocat/project", "--input", "-"])
        self.assertEqual(json.loads(cli.call_args_list[0].kwargs["input_data"]), {"description": "New description", "homepage": "https://example.com"})
        self.assertEqual(cli.call_args_list[1].args[0], ["--method", "PUT", "repos/octocat/project/topics", "--input", "-"])
        self.assertEqual(json.loads(cli.call_args_list[1].kwargs["input_data"]), {"names": ["python", "local-app"]})
        expected = copy.deepcopy(self.config)
        expected["repositories"][0].update({"description": "GitHub description", "homepage": "https://example.com/", "topics": ["local-app", "python"]})
        self.assertEqual(self.store.read()["config"], expected)
        self.assertEqual(result["repository"], expected["repositories"][0])
        self.assertEqual(result["revision"], self.store.read()["revision"])
        self.assertNotEqual(result["revision"], revision)
        # A pending local edit can use the new revision without overwriting About metadata.
        pending = copy.deepcopy(self.config)
        pending["comments"]["octocat/project"] = "Unsaved local note"
        saved = self.store.save(pending, result["revision"])
        self.assertEqual(saved["config"]["repositories"], expected["repositories"])
        self.assertEqual(saved["config"]["comments"], pending["comments"])

    def test_unchanged_about_skips_both_calls_and_preserves_revision(self):
        before = self.path.read_bytes()
        revision = self.store.read()["revision"]
        with patch.object(serve, "run_gh") as cli:
            status, result = self.request_about({"description": "A project", "homepage": "", "topics": []})
        cli.assert_not_called()
        self.assertEqual(status, 200)
        self.assertEqual(result["revision"], revision)
        self.assertEqual(self.path.read_bytes(), before)

    def test_about_skips_each_unchanged_group(self):
        with patch.object(serve, "run_gh", return_value=json.dumps({"names": ["python", "local-app"]})) as cli:
            status, _ = self.request_about({"description": "A project", "homepage": ""})
        self.assertEqual(status, 200)
        self.assertEqual(cli.call_count, 1)
        self.assertEqual(cli.call_args.args[0][1], "PUT")
        with patch.object(serve, "run_gh", return_value=json.dumps({"description": "New description", "homepage": None})) as cli:
            status, result = self.request_about({"homepage": "", "topics": ["local-app", "python"]})
        self.assertEqual(status, 200)
        self.assertEqual(cli.call_count, 1)
        self.assertEqual(cli.call_args.args[0][1], "PATCH")
        self.assertEqual(result["repository"]["homepage"], "")

    def test_about_rejects_archived_unknown_and_foreign_repositories(self):
        with patch.object(serve, "run_gh") as cli:
            for name in (None, "octocat/missing", "another-user/project", "octocat/../escape"):
                with self.subTest(name=name):
                    status, _ = self.request_about({"repository": name})
                    self.assertEqual(status, 400)
            self.config["repositories"][0]["archived"] = True
            self.path.write_text(json.dumps(self.config), encoding="utf-8")
            status, result = self.request_about()
        self.assertEqual(status, 400)
        self.assertIn("Archived", result["error"])
        cli.assert_not_called()

    def test_about_rejects_stale_revision_and_invalid_fields(self):
        before = self.path.read_bytes()
        with patch.object(serve, "run_gh") as cli:
            for revision in (None, "outdated"):
                status, _ = self.request_about({"revision": revision})
                self.assertEqual(status, 409)
            for change in ({"description": "x" * 351}, {"homepage": "javascript:alert(1)"}, {"topics": ["Bad"]}):
                status, _ = self.request_about(change)
                self.assertEqual(status, 400)
        cli.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_about_rejects_foreign_origin_and_host(self):
        with patch.object(serve, "run_gh") as cli:
            for headers in ({"Origin": "https://unrelated.example"}, {"Origin": "null"}, {"Host": "unrelated.example"}):
                status, _ = self.request_about(headers=headers)
                self.assertEqual(status, 403)
        cli.assert_not_called()

    def test_partial_failure_saves_description_and_returns_new_revision(self):
        revision = self.store.read()["revision"]
        replies = [json.dumps({"description": "Saved description", "homepage": "https://example.com"}), serve.GitHubError("topics denied")]
        with patch.object(serve, "run_gh", side_effect=replies):
            status, result = self.request_about()
        self.assertEqual(status, 502)
        self.assertIn("Description and website were saved", result["error"])
        self.assertIn("topics failed", result["error"])
        expected = copy.deepcopy(self.config)
        expected["repositories"][0].update({"description": "Saved description", "homepage": "https://example.com"})
        self.assertEqual(self.store.read()["config"], expected)
        self.assertEqual(result["repository"], expected["repositories"][0])
        self.assertNotEqual(result["revision"], revision)
        self.assertEqual(result["revision"], self.store.read()["revision"])

    def test_failed_first_call_does_not_change_config_or_send_topics(self):
        before = self.path.read_bytes()
        with patch.object(serve, "run_gh", side_effect=serve.GitHubError("permission denied")) as cli:
            status, result = self.request_about()
        self.assertEqual(status, 502)
        self.assertIn("description and website", result["error"])
        self.assertEqual(cli.call_count, 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_about_preserves_external_edit_during_github_call(self):
        changed = copy.deepcopy(self.config)
        changed["comments"]["octocat/project"] = "Saved externally"
        def external_edit(*args, **kwargs):
            self.path.write_text(json.dumps(changed), encoding="utf-8")
            return json.dumps({"description": "Saved description", "homepage": ""})
        with patch.object(serve, "run_gh", side_effect=external_edit):
            status, result = self.request_about({"homepage": "", "topics": []})
        self.assertEqual(status, 409)
        self.assertIn("GitHub was updated", result["error"])
        self.assertEqual(self.store.read()["config"], changed)

    def test_run_gh_passes_json_stdin_to_subprocess(self):
        payload = json.dumps({"description": "Quotes: \" and newlines\n", "homepage": ""})
        with patch.object(serve.shutil, "which", return_value="gh"), patch.object(serve.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as command:
            serve.run_gh(["--method", "PATCH", "repos/octocat/project", "--input", "-"], input_data=payload)
        self.assertEqual(command.call_args.kwargs["input"], payload)
        self.assertIn("--input", command.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
