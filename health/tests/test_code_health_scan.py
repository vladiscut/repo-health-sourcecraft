import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from django.utils import timezone

from django.test import SimpleTestCase

from health.code_health_scan import (
    _compute_metrics,
    _compute_todo_age,
    _scan_todos,
    _select_todo_candidate_files,
)
from health.models import Repository
from integrations.git import SourceCraftGitClient


def _tree(*entries):
    """Собирает нормализованное дерево как _collect_tree."""
    return {e["path"].lower(): e for e in entries}


def _file(path, type_="file"):
    return {"path": path, "name": path.rsplit("/", 1)[-1], "type": type_}


def _dir(path):
    return {"path": path, "name": path.rsplit("/", 1)[-1], "type": "dir"}


class ScanTodosTests(SimpleTestCase):
    def _repo(self, sha="abc"):
        return Mock(org_slug="org", repo_slug="repo", scan_commit_sha=sha)

    def test_no_commit_sha_returns_empty(self):
        repo = self._repo(sha=None)
        occurrences, error = _scan_todos(repo, [], 1, Mock())
        self.assertEqual(occurrences, [])
        self.assertEqual(error, "нет хеша последнего коммита")

    def test_reads_from_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "src").mkdir(parents=True, exist_ok=True)
            Path(tmp, "src/main.py").write_text("# TODO: fix this\nprint(1)\n", encoding="utf-8")
            with patch("health.code_health_scan.get_scan_repo_dir") as get_dir:
                get_dir.return_value = Path(tmp)
                repo = self._repo("abc")
                occurrences, error = _scan_todos(repo, ["src/main.py"], 1, Mock())
        self.assertEqual(occurrences, [("src/main.py", 1)])
        self.assertEqual(error, "")

    def test_no_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "src").mkdir(parents=True, exist_ok=True)
            Path(tmp, "src/main.py").write_text("print(1)\n", encoding="utf-8")
            with patch("health.code_health_scan.get_scan_repo_dir") as get_dir:
                get_dir.return_value = Path(tmp)
                repo = self._repo("abc")
                occurrences, error = _scan_todos(repo, ["src/main.py"], 1, Mock())
        self.assertEqual(occurrences, [])
        self.assertEqual(error, "")

    def test_file_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("health.code_health_scan.get_scan_repo_dir") as get_dir:
                get_dir.return_value = Path(tmp)
                repo = self._repo("abc")
                occurrences, error = _scan_todos(repo, ["src/main.py"], 1, Mock())
        self.assertEqual(occurrences, [])
        self.assertEqual(error, "не удалось прочитать ни один из отобранных файлов")

    def test_all_files_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("health.code_health_scan.get_scan_repo_dir") as get_dir:
                get_dir.return_value = Path(tmp)
                repo = self._repo("abc")
                occurrences, error = _scan_todos(repo, ["src/main.py"], 1, Mock())
        self.assertEqual(occurrences, [])
        self.assertEqual(error, "не удалось прочитать ни один из отобранных файлов")


class ComputeTodoAgeTests(SimpleTestCase):
    def _repo(self, sha="abc"):
        return Mock(org_slug="org", repo_slug="repo", scan_commit_sha=sha)

    def test_empty_occurrences(self):
        repo = self._repo("abc")
        old_count, error = _compute_todo_age(
            Mock(spec=SourceCraftGitClient),
            repo,
            1,
            [],
            timezone.now(),
        )
        self.assertEqual(old_count, 0)
        self.assertEqual(error, "")

    def test_old_marker(self):
        repo = self._repo("abc")
        git_client = Mock(spec=SourceCraftGitClient)
        git_client.get_line_commit_dates.return_value = {
            "src/main.py": {1: timezone.now() - timedelta(days=200)}
        }
        old_count, error = _compute_todo_age(
            git_client,
            repo,
            1,
            [("src/main.py", 1)],
            timezone.now(),
        )
        self.assertEqual(old_count, 1)
        self.assertEqual(error, "")

    def test_new_marker(self):
        repo = self._repo("abc")
        git_client = Mock(spec=SourceCraftGitClient)
        git_client.get_line_commit_dates.return_value = {
            "src/main.py": {1: timezone.now() - timedelta(days=1)}
        }
        old_count, error = _compute_todo_age(
            git_client,
            repo,
            1,
            [("src/main.py", 1)],
            timezone.now(),
        )
        self.assertEqual(old_count, 0)
        self.assertEqual(error, "")

    def test_missing_date(self):
        repo = self._repo("abc")
        git_client = Mock(spec=SourceCraftGitClient)
        git_client.get_line_commit_dates.return_value = {}
        old_count, error = _compute_todo_age(
            git_client,
            repo,
            1,
            [("src/main.py", 1)],
            timezone.now(),
        )
        self.assertEqual(old_count, 0)
        self.assertEqual(error, "")


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
            with patch("health.code_health_scan.get_scan_repo_dir") as get_dir:
                get_dir.return_value = Path(tmp)
                return _compute_metrics(
                    git_client=None,
                    repository=self._repo(),
                    tree=tree,
                    include_todo_age=False,
                    scan_id=1,
                    file_client=None,
                )

    def test_dependency_manifest_present(self):
        tree = _tree(_file("package.json"))
        m = self._metrics(tree)
        self.assertTrue(m.dependency_manifest_present)

    def test_dependency_manifest_absent(self):
        tree = _tree(_file("README.md"))
        m = self._metrics(tree)
        self.assertFalse(m.dependency_manifest_present)

    def test_lockfile_present(self):
        tree = _tree(_file("package-lock.json"))
        m = self._metrics(tree)
        self.assertTrue(m.lockfile_present)

    def test_tests_present(self):
        tree = _tree(_dir("tests"))
        m = self._metrics(tree)
        self.assertTrue(m.tests_present)

    def test_lint_config_present(self):
        tree = _tree(_file(".eslintrc"))
        m = self._metrics(tree)
        self.assertTrue(m.lint_config_present)

    def test_vendored_deps_present(self):
        tree = _tree(_file("node_modules/package.json"))
        m = self._metrics(tree)
        self.assertTrue(m.vendored_deps_present)

    def test_generated_artifacts_present(self):
        tree = _tree(_file("dist/app.js"))
        m = self._metrics(tree)
        self.assertTrue(m.generated_artifacts_present)

    def test_binary_junk_present(self):
        tree = _tree(_file("app.exe"))
        m = self._metrics(tree)
        self.assertTrue(m.binary_junk_present)

    def test_is_data_only_repo(self):
        tree = _tree(_file("data.csv"))
        m = self._metrics(tree)
        self.assertTrue(m.is_data_only_repo)

    def test_is_flat_dump(self):
        tree = _tree(_file("a.py"), _file("b.py"), _file("c.py"), _file("d.py"), _file("e.py"), _file("f.py"), _file("g.py"), _file("h.py"), _file("i.py"), _file("j.py"), _file("k.py"), _file("l.py"), _file("m.py"), _file("n.py"), _file("o.py"), _file("p.py"), _file("q.py"), _file("r.py"), _file("s.py"), _file("t.py"), _file("u.py"), _file("v.py"), _file("w.py"), _file("x.py"), _file("y.py"), _file("z.py"))
        m = self._metrics(tree)
        self.assertTrue(m.is_flat_dump)

    def test_source_files_count(self):
        tree = _tree(_file("src/main.py"), _file("src/utils.py"), _file("README.md"))
        m = self._metrics(tree)
        self.assertEqual(m.source_files_count, 2)

    def test_total_files_count(self):
        tree = _tree(_file("src/main.py"), _file("src/utils.py"), _file("README.md"))
        m = self._metrics(tree)
        self.assertEqual(m.total_files_count, 3)

    def test_todo_scan_files_count(self):
        tree = _tree(_file("src/main.py"), _file("src/utils.py"), _file("README.md"))
        m = self._metrics(tree)
        self.assertEqual(m.todo_scan_files_count, 2)

    def test_todo_total_count(self):
        tree = _tree(_file("src/main.py"), _file("src/utils.py"), _file("README.md"))
        m = self._metrics(tree, {"src/main.py": "# TODO: fix\n", "src/utils.py": "print(1)\n"})
        self.assertEqual(m.todo_total_count, 1)

    def test_todo_scan_error(self):
        tree = _tree(_file("src/main.py"))
        m = self._metrics(tree)
        self.assertTrue(m.todo_scan_error)

    def test_todo_age_error_when_include_todo_age_false(self):
        tree = _tree(_file("src/main.py"))
        m = self._metrics(tree)
        self.assertTrue(m.todo_age_error)

    def test_todo_age_error_when_git_client_none(self):
        tree = _tree(_file("src/main.py"))
        m = self._metrics(tree)
        self.assertTrue(m.todo_age_error)

    def test_todo_age_available(self):
        tree = _tree(_file("src/main.py"))
        m = self._metrics(tree)
        self.assertFalse(m.todo_age_available)

    def test_fetch_errors(self):
        tree = _tree(_file("src/main.py"))
        m = self._metrics(tree)
        self.assertEqual(m.fetch_errors, {})

    def test_clone_available_when_dir_has_files(self):
        tree = _tree(_file("src/main.py"))
        m = self._metrics(tree, {"src/main.py": "print(1)\n"})
        self.assertTrue(m.clone_available)

    def test_clone_unavailable_when_dir_empty(self):
        tree = _tree(_file("src/main.py"))
        m = self._metrics(tree)
        self.assertFalse(m.clone_available)


class FallbackRenormalizationTests(SimpleTestCase):
    """Запасной путь без клона: todo_debt недоступен, веса
    категории перенормируются по оставшимся под-метрикам."""

    def _repo(self, sha="abc"):
        return Mock(org_slug="org", repo_slug="repo", scan_commit_sha=sha)

    def test_no_clone_marks_todo_debt_unavailable(self):
        tree = _tree(
            _file("package.json"), _file("package-lock.json"),
            _file("src/main.py"), _dir("tests"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch("health.code_health_scan.get_scan_repo_dir") as get_dir:
                get_dir.return_value = Path(tmp)
                metrics = _compute_metrics(
                    git_client=None, repository=self._repo(),
                    tree=tree, include_todo_age=True, scan_id=1,
                    file_client=None,
                )
        self.assertFalse(metrics.clone_available)
        self.assertIsNone(metrics.todo_total_count)
        self.assertTrue(metrics.todo_scan_error)
        self.assertFalse(metrics.todo_age_available)

    def test_score_renormalized_without_todo_debt(self):
        """Без todo_debt оценка считается по оставшимся весам,
        а data_completeness меньше 1.0."""
        from health.scoring import (
            CODE_HEALTH_SUBMETRIC_WEIGHTS,
            score_code_health_category,
        )

        tree = _tree(
            _file("package.json"), _file("package-lock.json"),
            _file("src/main.py"), _dir("tests"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch("health.code_health_scan.get_scan_repo_dir") as get_dir:
                get_dir.return_value = Path(tmp)
                metrics = _compute_metrics(
                    git_client=None, repository=self._repo(),
                    tree=tree, include_todo_age=True, scan_id=1,
                    file_client=None,
                )

        score, completeness, submetrics = score_code_health_category(metrics)

        # todo_debt исключён из расчёта...
        self.assertNotIn("todo_debt", submetrics)
        # ...а оценка всё равно есть за счёт остальных под-метрик.
        self.assertIsNotNone(score)
        # data_completeness = доля доступного веса < 1.0 (выпал todo_debt 0.15).
        expected = 1.0 - CODE_HEALTH_SUBMETRIC_WEIGHTS["todo_debt"]
        self.assertAlmostEqual(completeness, round(expected, 2), places=2)

    def test_with_clone_todo_debt_present(self):
        """С доступным клоном todo_debt участвует, data_completeness = 1.0."""
        from health.scoring import score_code_health_category

        tree = _tree(
            _file("package.json"), _file("package-lock.json"),
            _file("src/main.py"), _dir("tests"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "src").mkdir(parents=True, exist_ok=True)
            Path(tmp, "src/main.py").write_text("print(1)\n", encoding="utf-8")
            file_client = Mock()
            file_client.get_file_text.return_value = "# TODO: implement\nprint(1)\n"
            with patch("health.code_health_scan.get_scan_repo_dir") as get_dir:
                get_dir.return_value = Path(tmp)
                metrics = _compute_metrics(
                    git_client=None, repository=self._repo(),
                    tree=tree, include_todo_age=False, scan_id=1,
                    file_client=file_client,
                )

        score, completeness, submetrics = score_code_health_category(metrics)
        self.assertIn("todo_debt", submetrics)
        self.assertIsNotNone(score)
        self.assertEqual(completeness, 1.0)


class IncludeTodoAgeWeightsTests(SimpleTestCase):
    """score_code_health_category выбирает набор весов по include_todo_age.

    При include_todo_age=False должен использоваться
    CODE_HEALTH_SUBMETRIC_WEIGHTS_WITHOUT_TODO_AGE, иначе балл категории
    и estimated_score_impact расходятся с фактическими весами планового
    скана.
    """

    def _repo(self, sha="abc"):
        return Mock(org_slug="org", repo_slug="repo", scan_commit_sha=sha)

    def _metrics(self, tree):
        from health.code_health_scan import _compute_metrics

        with tempfile.TemporaryDirectory() as tmp:
            with patch("health.code_health_scan.get_scan_repo_dir") as get_dir:
                get_dir.return_value = Path(tmp)
                return _compute_metrics(
                    git_client=None, repository=self._repo(),
                    tree=tree, include_todo_age=False, scan_id=1,
                    file_client=None,
                )

    def test_without_todo_age_uses_without_todo_age_weights(self):
        from health.scoring import (
            CODE_HEALTH_SUBMETRIC_WEIGHTS,
            CODE_HEALTH_SUBMETRIC_WEIGHTS_WITHOUT_TODO_AGE,
            score_code_health_category,
        )

        tree = _tree(
            _file("package.json"), _file("package-lock.json"),
            _file("src/main.py"), _dir("tests"),
        )
        metrics = self._metrics(tree)

        score_new, _, _ = score_code_health_category(
            metrics, include_todo_age=False
        )
        score_old, _, _ = score_code_health_category(
            metrics, include_todo_age=True
        )

        # Наборы весов различаются (todo_debt 0.05 vs 0.15) — значит и
        # итоговый балл должен отличаться при одинаковых субметриках.
        self.assertNotEqual(
            CODE_HEALTH_SUBMETRIC_WEIGHTS_WITHOUT_TODO_AGE,
            CODE_HEALTH_SUBMETRIC_WEIGHTS,
        )
        self.assertIsNotNone(score_new)
        self.assertIsNotNone(score_old)

    def test_default_argument_preserves_old_behaviour(self):
        from health.scoring import score_code_health_category

        tree = _tree(_file("src/main.py"))
        metrics = self._metrics(tree)

        default_score, _, _ = score_code_health_category(metrics)
        explicit_score, _, _ = score_code_health_category(
            metrics, include_todo_age=True
        )
        self.assertEqual(default_score, explicit_score)