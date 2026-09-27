"""Unit-тесты для :mod:`health.docs_scan`."""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from health.docs_scan import (
    _collect_tree,
    _compute_metrics,
    _detect_ci_config_present,
    _find_file_by_stem,
    _find_root_file,
    _has_dir,
    _has_path,
    _has_prefix_in,
    _has_sourcecraft_ci_config,
    _matches_any,
    _read_file_safe,
    run,
    run_docs_scan,
    README_MAX_CHARS,
)
from health.models import Finding, HealthScore, MetricSample, Scan
from health.tests.helpers import make_profile, make_repo
from integrations.sourcecraft import SourceCraftError


def _tree(*entries):
    """Собирает нормализованное дерево как _collect_tree."""
    return {e["path"].lower(): e for e in entries}


def _file(path, type_="file"):
    return {"path": path, "name": path.rsplit("/", 1)[-1], "type": type_}


def _dir(path):
    return {"path": path, "name": path.rsplit("/", 1)[-1], "type": "dir"}


class DocsHelpersTests(SimpleTestCase):
    # -- _has_sourcecraft_ci_config ------------------------------------

    def test_sourcecraft_ci_config_found(self):
        tree = _tree(_dir(".sourcecraft"), _file(".sourcecraft/ci.yaml"))
        self.assertTrue(_has_sourcecraft_ci_config(tree))

    def test_sourcecraft_ci_config_ignores_nested(self):
        tree = _tree(
            _dir(".sourcecraft"),
            _file(".sourcecraft/sub/ci.yaml"),
        )
        self.assertFalse(_has_sourcecraft_ci_config(tree))

    def test_sourcecraft_ci_config_wrong_stem(self):
        tree = _tree(_dir(".sourcecraft"), _file(".sourcecraft/pipeline.yaml"))
        self.assertFalse(_has_sourcecraft_ci_config(tree))

    # -- _detect_ci_config_present -------------------------------------

    def test_ci_config_gitlab(self):
        tree = _tree(_file(".gitlab-ci.yml"))
        self.assertTrue(_detect_ci_config_present(tree))

    def test_ci_config_github_workflows(self):
        tree = _tree(_dir(".github"), _file(".github/workflows/test.yml"))
        self.assertTrue(_detect_ci_config_present(tree))

    def test_ci_config_legacy_sourcecraft(self):
        tree = _tree(_file("sourcecraft-ci.yaml"))
        self.assertTrue(_detect_ci_config_present(tree))

    def test_ci_config_absent(self):
        tree = _tree(_file("README.md"))
        self.assertFalse(_detect_ci_config_present(tree))

    # -- _find_root_file -----------------------------------------------

    def test_find_root_file_only_root(self):
        tree = _tree(_file("docs/readme.md"), _file("README.md"))
        self.assertEqual(_find_root_file(tree, ("readme",)), "README.md")

    def test_find_root_file_respects_type(self):
        tree = _tree(_dir("readme"))
        self.assertIsNone(_find_root_file(tree, ("readme",)))

    def test_find_root_file_matches_name_with_extension(self):
        tree = _tree(_file("LICENSE.md"))
        self.assertEqual(_find_root_file(tree, ("license",)), "LICENSE.md")

    # -- _find_file_by_stem --------------------------------------------

    def test_find_file_by_stem_in_subdir(self):
        tree = _tree(_file(".github/CONTRIBUTING.md"))
        self.assertEqual(
            _find_file_by_stem(tree, ("contributing",)), ".github/CONTRIBUTING.md"
        )

    def test_find_file_by_stem_docs(self):
        tree = _tree(_file("docs/CONTRIBUTING.rst"))
        self.assertEqual(
            _find_file_by_stem(tree, ("contributing",)), "docs/CONTRIBUTING.rst"
        )

    # -- базовые проверки дерева ---------------------------------------

    def test_has_path(self):
        tree = _tree(_file("README.md"))
        self.assertTrue(_has_path(tree, "readme.md"))
        self.assertFalse(_has_path(tree, "missing.md"))

    def test_has_dir(self):
        tree = _tree(_dir("docs"), _file("README.md"))
        self.assertTrue(_has_dir(tree, "docs"))
        self.assertFalse(_has_dir(tree, "readme.md"))

    def test_has_prefix_in(self):
        tree = _tree(_file(".github/pull_request_template.md"))
        self.assertTrue(
            _has_prefix_in(tree, ".github", ("pull_request_template",))
        )
        self.assertFalse(_has_prefix_in(tree, ".github", ("issue_template",)))

    def test_matches_any_case_insensitive(self):
        self.assertTrue(_matches_any("QUICK START", [r"quick\s*start"]))
        self.assertFalse(_matches_any("nothing", [r"quick\s*start"]))


class DocsTreeTests(SimpleTestCase):
    def setUp(self):
        # Обходим Redis-кэш дерева: тесты должны ходить напрямую в мок-клиент.
        self._tree_patcher = patch(
            "health.docs_scan.get_repository_tree_cached",
            side_effect=lambda client, repo, *a, **kw: (
                client.get_repository_file_tree(repo.sourcecraft_id, None)
            ),
        )
        self._tree_patcher.start()
        self.addCleanup(self._tree_patcher.stop)

    def _repo(self):
        return Mock(
            org_slug="org",
            repo_slug="repo",
            sourcecraft_id="r",
            default_branch=None,
        )

    def test_collect_tree_lowercases_keys(self):
        client = Mock()
        client.get_repository_file_tree.return_value = [
            {"path": "README.md", "name": "README.md", "type": "file"},
        ]
        tree = _collect_tree(client, self._repo(), 1)
        self.assertIn("readme.md", tree)

    def test_collect_tree_returns_none_on_error(self):
        client = Mock()
        client.get_repository_file_tree.side_effect = SourceCraftError("boom")
        tree = _collect_tree(client, self._repo(), 1)
        self.assertIsNone(tree)

    def test_collect_tree_skips_entries_without_path(self):
        client = Mock()
        client.get_repository_file_tree.return_value = [
            {"path": "", "name": "", "type": "file"},
            {"path": "README.md", "name": "README.md", "type": "file"},
        ]
        tree = _collect_tree(client, self._repo(), 1)
        self.assertEqual(len(tree), 1)


class ReadFileSafeTests(SimpleTestCase):
    def _repo(self, sha=None):
        return Mock(
            org_slug="org", repo_slug="repo", scan_commit_sha=sha
        )

    def test_missing_file(self):
        with patch("health.docs_scan.get_scan_repo_dir") as get_dir:
            get_dir.return_value = Path("/tmp/nonexistent-scan-clone")
            content, reason = _read_file_safe(1, self._repo(None), "README.md", Mock())
        self.assertIsNone(content)
        self.assertEqual(reason, "файл не найден в клоне")

    def test_reads_from_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "README.md").write_text("# Project\n", encoding="utf-8")
            with patch("health.docs_scan.get_scan_repo_dir") as get_dir:
                get_dir.return_value = Path(tmp)
                content, reason = _read_file_safe(1, self._repo("abc"), "README.md", Mock())
        self.assertEqual(content, "# Project\n")
        self.assertEqual(reason, "")

    def test_truncates_to_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "README.md").write_text(
                "x" * (README_MAX_CHARS + 100), encoding="utf-8"
            )
            with patch("health.docs_scan.get_scan_repo_dir") as get_dir:
                get_dir.return_value = Path(tmp)
                content, reason = _read_file_safe(1, self._repo("abc"), "README.md", Mock())
        self.assertEqual(len(content), README_MAX_CHARS)
        self.assertEqual(reason, "")


class ComputeMetricsTests(SimpleTestCase):
    def _repo(self, sha="abc"):
        return Mock(org_slug="org", repo_slug="repo", scan_commit_sha=sha)

    def _metrics(self, tree, files=None):
        """Прогон _compute_metrics с клоном, собранным из словаря files."""
        files = files or {}
        with tempfile.TemporaryDirectory() as tmp:
            for path, content in files.items():
                full = Path(tmp, path)
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_text(content, encoding="utf-8")
            with patch("health.docs_scan.get_scan_repo_dir") as get_dir:
                get_dir.return_value = Path(tmp)
                return _compute_metrics(1, self._repo(), tree, Mock())

    def test_readme_present_metrics(self):
        content = (
            "# Project\n## Quick Start\npip install -r requirements.txt\n"
            "## Build\npytest\n## Structure\nlayout here\n"
        )
        tree = _tree(_file("README.md"))
        m = self._metrics(tree, {"README.md": content})
        self.assertTrue(m.readme_present)
        self.assertEqual(m.readme_path, "README.md")
        self.assertEqual(m.readme_size_chars, len(content))
        self.assertTrue(m.readme_has_local_run)
        self.assertTrue(m.readme_has_build_test)
        self.assertTrue(m.readme_has_structure)

    def test_readme_absent(self):
        tree = _tree(_file("main.py"))
        m = self._metrics(tree)
        self.assertFalse(m.readme_present)
        self.assertIsNone(m.readme_path)

    def test_readme_not_read(self):
        tree = _tree(_file("README.md"))
        m = self._metrics(tree)
        self.assertTrue(m.readme_present)
        self.assertTrue(m.readme_read_error)
        self.assertIsNone(m.readme_size_chars)

    def test_license_recognized_mit(self):
        tree = _tree(_file("LICENSE"))
        m = self._metrics(tree, {"LICENSE": "MIT License\n..."})
        self.assertTrue(m.license_present)
        self.assertTrue(m.license_type_recognized)

    def test_license_not_recognized(self):
        tree = _tree(_file("LICENSE"))
        m = self._metrics(tree, {"LICENSE": "Custom license text"})
        self.assertTrue(m.license_present)
        self.assertFalse(m.license_type_recognized)

    def test_license_not_read(self):
        tree = _tree(_file("LICENSE"))
        m = self._metrics(tree)
        self.assertTrue(m.license_present)
        self.assertTrue(m.license_read_error)
        self.assertIsNone(m.license_type_recognized)

    def test_contributing_present(self):
        tree = _tree(_file("CONTRIBUTING.md"))
        m = self._metrics(tree)
        self.assertTrue(m.contributing_present)

    def test_changelog_and_docs_dir(self):
        tree = _tree(_file("CHANGELOG.md"), _dir("docs"))
        m = self._metrics(tree)
        self.assertTrue(m.changelog_present)
        self.assertTrue(m.docs_dir_present)

    def test_codeowners_paths(self):
        tree = _tree(_file(".github/CODEOWNERS"))
        m = self._metrics(tree)
        self.assertTrue(m.codeowners_present)

    def test_issue_templates_present(self):
        tree = _tree(
            _file(".github/issue_template.md"),
            _file(".github/ISSUE_TEMPLATE/bug.md"),
        )
        m = self._metrics(tree)
        self.assertTrue(m.issue_templates_present)

    def test_pr_template_present(self):
        tree = _tree(_file(".github/pull_request_template.md"))
        m = self._metrics(tree)
        self.assertTrue(m.pr_template_present)

    def test_ci_config_present_delegated(self):
        tree = _tree(_dir(".sourcecraft"), _file(".sourcecraft/ci.yaml"))
        m = self._metrics(tree)
        self.assertTrue(m.ci_config_present)


class _TempClone:
    """Контекст-менеджер: временный клон + патч get_scan_repo_dir."""

    def __init__(self, files):
        self.files = files
        self._tmp = None

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        for path, content in self.files.items():
            full = root / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")
        self._patcher = patch("health.docs_scan.get_scan_repo_dir")
        mock = self._patcher.start()
        mock.return_value = root
        return root

    def __exit__(self, *exc):
        self._patcher.stop()
        self._tmp.cleanup()
        return False

class DocsScanTests(TestCase):
    def setUp(self):
        # Обходим Redis-кэш дерева: тесты должны ходить напрямую в мок-клиент.
        self._tree_patcher = patch(
            "health.docs_scan.get_repository_tree_cached",
            side_effect=lambda client, repo, *a, **kw: (
                client.get_repository_file_tree(repo.sourcecraft_id, None)
            ),
        )
        self._tree_patcher.start()
        self.addCleanup(self._tree_patcher.stop)

    def _scan(self, **kwargs):
        repo = make_repo()
        return Scan.objects.create(
            repository=repo, commit_sha_at_analysis="abc", **kwargs
        )

    def _client(self, tree=None):
        client = Mock()
        if tree is None:
            client.get_repository_file_tree.side_effect = SourceCraftError("boom")
        else:
            client.get_repository_file_tree.return_value = tree
        return client

    def _clone(self, files=None):
        """Контекст-менеджер: временный клон + патч get_scan_repo_dir."""
        return _TempClone(files or {})

    def _file_client(self, files=None):
        """Мок файлового клиента: отдаёт содержимое по пути из files."""
        files = files or {}
        client = Mock()
        client.get_file_text.side_effect = lambda org, slug, path, sha: (
            files[path]
        )
        return client

    def test_tree_none_writes_fetch_error(self):
        scan = self._scan()
        client = self._client(tree=None)
        run_docs_scan(scan, client, None)
        sample = MetricSample.objects.get(
            scan=scan, metric_key="docs_fetch_error"
        )
        self.assertFalse(sample.is_available)
        hs = HealthScore.objects.get(scan=scan, category=MetricSample.Category.DOCS)
        self.assertIsNone(hs.total)

    def test_success_writes_health_score(self):
        scan = self._scan()
        tree = [_file("README.md"), _file("LICENSE")]
        client = self._client(tree=tree)
        file_client = self._file_client(
            {"README.md": "# Project\n", "LICENSE": "MIT License"}
        )
        run_docs_scan(scan, client, file_client)
        hs = HealthScore.objects.get(scan=scan, category=MetricSample.Category.DOCS)
        self.assertIn("readme_present", hs.raw_metrics)
        self.assertIn("submetric_scores", hs.raw_metrics)

    def test_findings_no_readme(self):
        scan = self._scan()
        client = self._client(tree=[_file("main.py")])
        run_docs_scan(scan, client, None)
        self.assertTrue(
            Finding.objects.filter(
                scan=scan,
                category=MetricSample.Category.DOCS,
                title="Отсутствует README",
            ).exists()
        )

    def test_run_returns_pk(self):
        scan = self._scan()
        with self._clone({"README.md": "# Project\n"}), patch(
            "health.docs_scan.SourceCraftClient"
        ) as c_cls:
            c = c_cls.return_value
            c.get_repository_file_tree.return_value = [_file("README.md")]
            pk = run(scan.pk)
        self.assertEqual(pk, HealthScore.objects.get(pk=pk).pk)

    def test_run_user_scan_passes_user_token(self):
        user = get_user_model().objects.create_user("owner", password="x")
        make_profile(user, sourcecraft_pat="pat-user-123")
        scan = self._scan(
            triggered_by=Scan.TriggeredBy.USER,
            triggered_by_user=user,
        )
        with self._clone({"README.md": "# Project\n"}), patch(
            "health.docs_scan.SourceCraftClient"
        ) as c_cls:
            c_cls.return_value.get_repository_file_tree.return_value = [
                _file("README.md")
            ]
            run(scan.pk)
        c_cls.assert_called_once_with(token="pat-user-123")

    def test_run_scheduled_scan_passes_no_token(self):
        scan = self._scan()
        with self._clone({"README.md": "# Project\n"}), patch(
            "health.docs_scan.SourceCraftClient"
        ) as c_cls:
            c_cls.return_value.get_repository_file_tree.return_value = [
                _file("README.md")
            ]
            run(scan.pk)
        c_cls.assert_called_once_with(token=None)
