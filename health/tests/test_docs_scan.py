"""Unit-тесты для :mod:`health.docs_scan`."""

from unittest.mock import Mock, patch

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
from health.tests.helpers import make_repo
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

    def test_find_root_file_suffixes(self):
        tree = _tree(_file("LICENSE.md"))
        self.assertEqual(
            _find_root_file(tree, ("license",), suffixes=(".md",)), "LICENSE.md"
        )

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
    def test_collect_tree_lowercases_keys(self):
        client = Mock()
        client.get_repository_file_tree.return_value = [
            {"path": "README.md", "name": "README.md", "type": "file"},
        ]
        tree = _collect_tree(client, Mock(sourcecraft_id="r", default_branch=None))
        self.assertIn("readme.md", tree)

    def test_collect_tree_returns_none_on_error(self):
        client = Mock()
        client.get_repository_file_tree.side_effect = SourceCraftError("boom")
        tree = _collect_tree(client, Mock(sourcecraft_id="r", default_branch=None))
        self.assertIsNone(tree)

    def test_collect_tree_skips_entries_without_path(self):
        client = Mock()
        client.get_repository_file_tree.return_value = [
            {"path": "", "name": "", "type": "file"},
            {"path": "README.md", "name": "README.md", "type": "file"},
        ]
        tree = _collect_tree(client, Mock(sourcecraft_id="r", default_branch=None))
        self.assertEqual(len(tree), 1)


class ReadFileSafeTests(SimpleTestCase):
    def _repo(self, sha=None):
        return Mock(
            org_slug="org", repo_slug="repo", scan_commit_sha=sha
        )

    def test_no_commit_sha(self):
        content, reason = _read_file_safe(Mock(), self._repo(None), "/README.md")
        self.assertIsNone(content)
        self.assertEqual(reason, "нет хеша последнего коммита")

    def test_404(self):
        client = Mock()
        client.get_file_text.side_effect = SourceCraftError("no", status_code=404)
        content, reason = _read_file_safe(client, self._repo("abc"), "/README.md")
        self.assertIsNone(content)
        self.assertEqual(reason, "файл не найден (404)")

    def test_truncates_to_max(self):
        client = Mock()
        client.get_file_text.return_value = "x" * (README_MAX_CHARS + 100)
        content, reason = _read_file_safe(client, self._repo("abc"), "/README.md")
        self.assertEqual(len(content), README_MAX_CHARS)
        self.assertEqual(reason, "")

    def test_other_error(self):
        client = Mock()
        client.get_file_text.side_effect = SourceCraftError("boom", status_code=500)
        content, reason = _read_file_safe(client, self._repo("abc"), "/README.md")
        self.assertIsNone(content)
        self.assertTrue(reason.startswith("ошибка API:"))


class ComputeMetricsTests(SimpleTestCase):
    def _repo(self, sha="abc"):
        return Mock(org_slug="org", repo_slug="repo", scan_commit_sha=sha)

    def _file_client(self, content="# Project\n"):
        client = Mock()
        client.get_file_text.return_value = content
        return client

    def test_readme_present_metrics(self):
        content = (
            "# Project\n## Quick Start\npip install -r requirements.txt\n"
            "## Build\npytest\n## Structure\nlayout here\n"
        )
        tree = _tree(_file("README.md"))
        m = _compute_metrics(self._file_client(content), self._repo(), tree)
        self.assertTrue(m.readme_present)
        self.assertEqual(m.readme_path, "README.md")
        self.assertEqual(m.readme_size_chars, len(content))
        self.assertTrue(m.readme_has_local_run)
        self.assertTrue(m.readme_has_build_test)
        self.assertTrue(m.readme_has_structure)

    def test_readme_absent(self):
        tree = _tree(_file("main.py"))
        m = _compute_metrics(self._file_client(), self._repo(), tree)
        self.assertFalse(m.readme_present)
        self.assertIsNone(m.readme_path)

    def test_readme_not_read(self):
        client = Mock()
        client.get_file_text.side_effect = SourceCraftError("boom", status_code=500)
        tree = _tree(_file("README.md"))
        m = _compute_metrics(client, self._repo(), tree)
        self.assertTrue(m.readme_present)
        self.assertTrue(m.readme_read_error)
        self.assertIsNone(m.readme_size_chars)

    def test_license_recognized_mit(self):
        client = Mock()
        client.get_file_text.return_value = "MIT License\n..."
        tree = _tree(_file("LICENSE"))
        m = _compute_metrics(client, self._repo(), tree)
        self.assertTrue(m.license_present)
        self.assertTrue(m.license_type_recognized)

    def test_license_not_recognized(self):
        client = Mock()
        client.get_file_text.return_value = "Custom license text"
        tree = _tree(_file("LICENSE"))
        m = _compute_metrics(client, self._repo(), tree)
        self.assertTrue(m.license_present)
        self.assertFalse(m.license_type_recognized)

    def test_license_not_read(self):
        client = Mock()
        client.get_file_text.side_effect = SourceCraftError("boom", status_code=500)
        tree = _tree(_file("LICENSE"))
        m = _compute_metrics(client, self._repo(), tree)
        self.assertTrue(m.license_present)
        self.assertTrue(m.license_read_error)
        self.assertIsNone(m.license_type_recognized)

    def test_contributing_present(self):
        tree = _tree(_file("CONTRIBUTING.md"))
        m = _compute_metrics(self._file_client(), self._repo(), tree)
        self.assertTrue(m.contributing_present)

    def test_changelog_and_docs_dir(self):
        tree = _tree(_file("CHANGELOG.md"), _dir("docs"))
        m = _compute_metrics(self._file_client(), self._repo(), tree)
        self.assertTrue(m.changelog_present)
        self.assertTrue(m.docs_dir_present)

    def test_codeowners_paths(self):
        tree = _tree(_file(".github/CODEOWNERS"))
        m = _compute_metrics(self._file_client(), self._repo(), tree)
        self.assertTrue(m.codeowners_present)

    def test_issue_templates_present(self):
        tree = _tree(
            _file(".github/issue_template.md"),
            _file(".github/ISSUE_TEMPLATE/bug.md"),
        )
        m = _compute_metrics(self._file_client(), self._repo(), tree)
        self.assertTrue(m.issue_templates_present)

    def test_pr_template_present(self):
        tree = _tree(_file(".github/pull_request_template.md"))
        m = _compute_metrics(self._file_client(), self._repo(), tree)
        self.assertTrue(m.pr_template_present)

    def test_ci_config_present_delegated(self):
        tree = _tree(_dir(".sourcecraft"), _file(".sourcecraft/ci.yaml"))
        m = _compute_metrics(self._file_client(), self._repo(), tree)
        self.assertTrue(m.ci_config_present)


class DocsScanTests(TestCase):
    def _scan(self):
        repo = make_repo()
        return Scan.objects.create(repository=repo, commit_sha_at_analysis="abc")

    def _clients(self, tree=None, content="# Project\n"):
        client = Mock()
        if tree is None:
            client.get_repository_file_tree.side_effect = SourceCraftError("boom")
        else:
            client.get_repository_file_tree.return_value = tree
        file_client = Mock()
        file_client.get_file_text.return_value = content
        return client, file_client

    def test_tree_none_writes_fetch_error(self):
        scan = self._scan()
        client, file_client = self._clients(tree=None)
        run_docs_scan(scan, client, file_client)
        sample = MetricSample.objects.get(
            scan=scan, metric_key="docs_fetch_error"
        )
        self.assertFalse(sample.is_available)
        hs = HealthScore.objects.get(scan=scan, category=MetricSample.Category.DOCS)
        self.assertIsNone(hs.total)

    def test_success_writes_health_score(self):
        scan = self._scan()
        tree = [_file("README.md"), _file("LICENSE")]
        client, file_client = self._clients(tree=tree, content="MIT License")
        run_docs_scan(scan, client, file_client)
        hs = HealthScore.objects.get(scan=scan, category=MetricSample.Category.DOCS)
        self.assertIn("readme_present", hs.raw_metrics)
        self.assertIn("submetric_scores", hs.raw_metrics)

    def test_scan_commit_sha_taken_from_scan(self):
        scan = self._scan()
        tree = [_file("README.md")]
        client, file_client = self._clients(tree=tree)
        run_docs_scan(scan, client, file_client)
        file_client.get_file_text.assert_any_call(
            "acme", scan.repository.repo_slug, "README.md", "abc"
        )

    def test_findings_no_readme(self):
        scan = self._scan()
        tree = [_file("main.py")]
        client, file_client = self._clients(tree=tree)
        run_docs_scan(scan, client, file_client)
        self.assertTrue(
            Finding.objects.filter(
                scan=scan,
                category=MetricSample.Category.DOCS,
                title="Отсутствует README",
            ).exists()
        )

    def test_run_returns_pk(self):
        scan = self._scan()
        with patch("health.docs_scan.SourceCraftClient") as c_cls, patch(
            "health.docs_scan.SourceCraftFileClient"
        ) as f_cls:
            c = c_cls.return_value
            c.get_repository_file_tree.return_value = [_file("README.md")]
            f = f_cls.return_value
            f.get_file_text.return_value = "# Project\n"
            pk = run(scan.pk)
        self.assertEqual(pk, HealthScore.objects.get(pk=pk).pk)
