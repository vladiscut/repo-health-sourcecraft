import logging
import re

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from health.models import (
    Finding,
    HealthScore,
    MetricSample,
    Scan,
    Repository,
)
from health.scoring import (
    CATEGORY_WEIGHTS,
    CODE_HEALTH_CATEGORY_SCORE_SEVERITY_BUMP_THRESHOLD,
    CODE_HEALTH_SUBMETRIC_WEIGHTS,
    CODE_HEALTH_SUBMETRIC_WEIGHTS_WITHOUT_TODO_AGE,
    CODE_HEALTH_TODO_OLD_AGE_DAYS,
    bump_severity,
    find_impact,
    score_code_health_category,
)
from health.tree_cache import get_repository_tree_cached
from integrations.sourcecraft import (
    SourceCraftClient,
    SourceCraftError,
    SourceCraftFileClient,
)
from integrations.git import SourceCraftGitClient, get_scan_repo_dir


logger = logging.getLogger(__name__)

CATEGORY = MetricSample.Category.CODE_HEALTH

CATEGORY_WEIGHT = CATEGORY_WEIGHTS[CATEGORY]

CODE_HEALTH_MAX_TODO_SCAN_FILES = 30
CODE_HEALTH_FILE_MAX_CHARS = 50_000

DEPENDENCY_MANIFEST_NAMES = frozenset({
    "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "requirements.txt", "pipfile", "go.mod", "cargo.toml", "gemfile",
    "composer.json", "pom.xml", "build.gradle", "build.gradle.kts",
    "mix.exs", "project.clj", "pubspec.yaml",
})

LOCKFILE_NAMES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "pipfile.lock", "go.sum", "cargo.lock", "composer.lock",
    "gemfile.lock", "mix.lock", "pubspec.lock",
})

TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__", "spec", "specs"})
TEST_FILE_PATTERNS = [
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"(^|/)[^/]+_test\.py$"),
    re.compile(r"(^|/)[^/]+\.test\.[jt]sx?$"),
    re.compile(r"(^|/)[^/]+\.spec\.[jt]sx?$"),
    re.compile(r"(^|/)[^/]+_test\.go$"),
    re.compile(r"(^|/)[^/]+test\.rb$"),
]

LINT_CONFIG_NAMES = frozenset({
    ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json",
    ".eslintrc.yml", ".eslintrc.yaml", ".flake8", ".pylintrc",
    "ruff.toml", ".ruff.toml", ".prettierrc", ".prettierrc.json",
    ".prettierrc.yml", ".editorconfig", "rustfmt.toml",
    ".golangci.yml", ".golangci.yaml", "tslint.json", "checkstyle.xml",
    ".rubocop.yml", ".stylelintrc", ".stylelintrc.json",
})

VENDORED_DIR_NAMES = frozenset({
    "node_modules", "vendor", "venv", ".venv", "env", "site-packages",
    "bower_components", ".gradle", "__pycache__", ".tox", "pods",
})

GENERATED_DIR_NAMES = frozenset({
    "dist", "build", "out", "target", ".next", ".nuxt", ".cache", "coverage",
})

BINARY_JUNK_SUFFIXES = (
    ".pyc", ".pyo", ".class", ".o", ".obj", ".so", ".dll", ".dylib",
    ".exe", ".jar", ".war", ".ear", ".min.js", ".min.css", ".map",
)

DATA_ONLY_SUFFIXES = (
    ".csv", ".tsv", ".json", ".parquet", ".xlsx", ".xls", ".jsonl"
)

SOURCE_CODE_SUFFIXES = (
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".kt",
    ".rb", ".php", ".c", ".cpp", ".h", ".hpp", ".cs", ".swift",
    ".scala", ".m", ".sh", ".pl", ".lua", ".ex", ".exs", ".dart", ".vue",
)

TODO_MARKER_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b", re.IGNORECASE)


def _weights_for(include_todo_age: bool) -> dict[str, float]:
    return (
        CODE_HEALTH_SUBMETRIC_WEIGHTS
        if include_todo_age
        else CODE_HEALTH_SUBMETRIC_WEIGHTS_WITHOUT_TODO_AGE
    )


@dataclass
class _CodeHealthMetrics:
    dependency_manifest_present: bool | None = None
    lockfile_present: bool | None = None
    tests_present: bool | None = None
    lint_config_present: bool | None = None

    vendored_deps_present: bool | None = None
    generated_artifacts_present: bool | None = None
    binary_junk_present: bool | None = None

    is_data_only_repo: bool | None = None
    is_flat_dump: bool | None = None
    source_files_count: int | None = None
    total_files_count: int | None = None

    todo_total_count: int | None = None
    todo_old_count: int | None = None
    todo_scan_files_count: int | None = None
    todo_scan_error: str = ""
    todo_age_available: bool = False
    todo_age_error: str = ""

    clone_available: bool = False

    fetch_errors: dict[str, str] = field(default_factory=dict)


def _is_junk_dir(path_lower: str, junk_dirs: frozenset[str]) -> bool:
    parts = path_lower.split("/")
    return any(part in junk_dirs for part in parts[:-1])


def _detect_dependency_manifest(tree: dict[str, dict[str, Any]]) -> bool:
    return any(
        path_lower.rsplit("/", 1)[-1] in DEPENDENCY_MANIFEST_NAMES
        for path_lower in tree
    )


def _detect_lockfile(tree: dict[str, dict[str, Any]]) -> bool:
    return any(
        path_lower.rsplit("/", 1)[-1] in LOCKFILE_NAMES
        for path_lower in tree
    )


def _detect_tests(tree: dict[str, dict[str, Any]]) -> bool:
    for path_lower, entry in tree.items():
        entry_type = str(entry.get("type") or "").lower()
        parts = path_lower.split("/")
        if entry_type == "dir" and parts[-1] in TEST_DIR_NAMES:
            return True
        if any(part in TEST_DIR_NAMES for part in parts[:-1]):
            return True
        if any(pattern.search(path_lower) for pattern in TEST_FILE_PATTERNS):
            return True
    return False


def _detect_lint_config(tree: dict[str, dict[str, Any]]) -> bool:
    return any(
        path_lower.rsplit("/", 1)[-1] in LINT_CONFIG_NAMES
        for path_lower in tree
    )


def _detect_vendored(tree: dict[str, dict[str, Any]]) -> bool:
    return any(_is_junk_dir(p, VENDORED_DIR_NAMES) for p in tree)


def _detect_generated(tree: dict[str, dict[str, Any]]) -> bool:
    return any(_is_junk_dir(p, GENERATED_DIR_NAMES) for p in tree)


def _detect_binary_junk(tree: dict[str, dict[str, Any]]) -> bool:
    return any(
        path_lower.endswith(BINARY_JUNK_SUFFIXES) for path_lower in tree
    )


def _classify_structure(
    tree: dict[str, dict[str, Any]],
) -> tuple[bool, bool, int, int]:
    files = [
        (p, e) for p, e in tree.items()
        if str(e.get("type") or "").lower() in ("file", "executable")
    ]
    total_files_count = len(files)
    source_files_count = sum(
        1 for p, _ in files if p.endswith(SOURCE_CODE_SUFFIXES)
    )
    data_files_count = sum(
        1 for p, _ in files if p.endswith(DATA_ONLY_SUFFIXES)
    )

    is_data_only = (
        total_files_count > 0
        and source_files_count == 0
        and data_files_count > 0
    )

    root_files = sum(1 for p, _ in files if "/" not in p)
    top_level_dirs = {
        p.split("/", 1)[0] for p, e in tree.items() if "/" in p
    }
    is_flat_dump = (
        total_files_count > 15
        and root_files >= max(10, int(total_files_count * 0.6))
        and len(top_level_dirs) <= 1
    )

    return is_data_only, is_flat_dump, source_files_count, total_files_count


def _select_todo_candidate_files(tree: dict[str, dict[str, Any]]) -> list[str]:
    candidates: list[str] = []
    for path_lower, entry in tree.items():
        entry_type = str(entry.get("type") or "").lower()
        if entry_type not in ("file", "executable"):
            continue
        if not path_lower.endswith(SOURCE_CODE_SUFFIXES):
            continue
        if _is_junk_dir(path_lower, VENDORED_DIR_NAMES | GENERATED_DIR_NAMES):
            continue
        candidates.append(str(entry.get("path") or entry.get("name")))
        if len(candidates) >= CODE_HEALTH_MAX_TODO_SCAN_FILES:
            break
    return candidates


def _scan_todos(
    repo: Repository,
    candidate_paths: list[str],
    scan_id: int,
    file_client: SourceCraftFileClient,
) -> tuple[list[tuple[str, int]], str]:
    if not repo.scan_commit_sha:
        return [], "нет хеша последнего коммита"

    repo_dir = None
    if repo.visibility != Repository.VisibilityType.PUBLIC:
        repo_dir = Path(get_scan_repo_dir(scan_id))
        if not repo_dir.is_dir():
            return [], "клон репозитория недоступен"

    occurrences: list[tuple[str, int]] = []
    errors = 0
    for path in candidate_paths:
        if repo.visibility == Repository.VisibilityType.PUBLIC:
            try:
                content = file_client.get_file_text(
                    repo.org_slug, repo.repo_slug, path, repo.scan_commit_sha
                )
            except SourceCraftError as exc:
                if exc.status_code == 404:
                    return None, "файл не найден (404)"
                return None, f"ошибка API: {exc}"
        else:
            try:
                repo_path = repo_dir / path
                with open(repo_path, "r", encoding="utf-8") as file:
                    content = file.read()
            except (SourceCraftError, OSError) as exc:
                errors += 1
                logger.debug(
                    f"Не удалось прочитать {path} для TODO-скана: {exc}"
                )
                continue

        if len(content) > CODE_HEALTH_FILE_MAX_CHARS:
            content = content[:CODE_HEALTH_FILE_MAX_CHARS]

        for line_no, line in enumerate(content.splitlines(), start=1):
            if TODO_MARKER_RE.search(line):
                occurrences.append((path, line_no))

    scan_error = ""
    if candidate_paths and errors == len(candidate_paths):
        scan_error = "не удалось прочитать ни один из отобранных файлов"
    return occurrences, scan_error


def _compute_todo_age(
    git_client: SourceCraftGitClient,
    repository: Repository,
    scan_id: int,
    occurrences: list[tuple[str, int]],
    now,
) -> tuple[int | None, str]:
    """Возвращает (todo_old_count, error) по давности TODO через git blame"""

    line_specs: dict[str, list[int]] = {}
    for path, line_no in occurrences:
        line_specs.setdefault(path, []).append(line_no)

    try:
        dates_by_path = git_client.get_line_commit_dates(
            scan_id=scan_id,
            branch=repository.default_branch,
            line_specs=line_specs,
        )
    except SourceCraftError as exc:
        logger.warning(f"Не удалось получить давность TODO для {repository}: {exc}")
        return None, str(exc)

    threshold = now - timedelta(days=CODE_HEALTH_TODO_OLD_AGE_DAYS)
    old_count = 0
    for path, line_no in occurrences:
        commit_date = dates_by_path.get(path, {}).get(line_no)
        if commit_date is not None and commit_date < threshold:
            old_count += 1
    return old_count, ""


def _compute_metrics(
    git_client: SourceCraftGitClient | None,
    repository: Repository,
    tree: dict[str, dict[str, Any]],
    include_todo_age: bool,
    scan_id: int,
    file_client: SourceCraftFileClient,
) -> _CodeHealthMetrics:
    metrics = _CodeHealthMetrics()

    metrics.dependency_manifest_present = _detect_dependency_manifest(tree)
    metrics.lockfile_present = _detect_lockfile(tree)
    metrics.tests_present = _detect_tests(tree)
    metrics.lint_config_present = _detect_lint_config(tree)

    metrics.vendored_deps_present = _detect_vendored(tree)
    metrics.generated_artifacts_present = _detect_generated(tree)
    metrics.binary_junk_present = _detect_binary_junk(tree)

    is_data_only, is_flat_dump, source_count, total_count = _classify_structure(tree)
    metrics.is_data_only_repo = is_data_only
    metrics.is_flat_dump = is_flat_dump
    metrics.source_files_count = source_count
    metrics.total_files_count = total_count

    candidate_paths = _select_todo_candidate_files(tree)
    repo_dir = Path(get_scan_repo_dir(scan_id))
    metrics.clone_available = repo_dir.is_dir() and any(repo_dir.iterdir())

    occurrences, scan_error = _scan_todos(
        repository, candidate_paths, scan_id, file_client
    )
    metrics.todo_scan_files_count = len(candidate_paths)
    metrics.todo_scan_error = scan_error
    metrics.todo_total_count = None if scan_error else len(occurrences)

    if scan_error:
        metrics.todo_age_error = scan_error
    elif not occurrences:
        metrics.todo_old_count = 0
        metrics.todo_age_available = True
    else:
        if include_todo_age:
            old_count, error = _compute_todo_age(
                git_client, repository, scan_id, occurrences, timezone.now()
            )
            if error:
                metrics.todo_age_error = error
                metrics.fetch_errors["todo_age"] = error
            else:
                metrics.todo_old_count = old_count
                metrics.todo_age_available = True
        else:
            metrics.todo_age_error = "давность TODO не считается при массовом плановом скане"

    return metrics


def _save_metric_samples(scan: Scan, metrics: _CodeHealthMetrics) -> None:
    def _save(
        key: str,
        value: Any,
        unit: str = "",
        is_available: bool = True,
        reason: str = "",
        source_reference: str = "",
    ) -> None:
        reference = (source_reference or "").strip()
        MetricSample.objects.update_or_create(
            scan=scan,
            category=CATEGORY,
            metric_key=key,
            defaults=dict(
                value=value,
                unit=unit,
                is_available=is_available,
                error_reason=reason,
                source_reference=reference[:500],
            ),
        )

    repo_url = (scan.repository.url or "").strip()

    for key, value in (
        ("code_health_dependency_manifest_present", metrics.dependency_manifest_present),
        ("code_health_lockfile_present", metrics.lockfile_present),
        ("code_health_tests_present", metrics.tests_present),
        ("code_health_lint_config_present", metrics.lint_config_present),
        ("code_health_vendored_deps_present", metrics.vendored_deps_present),
        ("code_health_generated_artifacts_present", metrics.generated_artifacts_present),
        ("code_health_binary_junk_present", metrics.binary_junk_present),
        ("code_health_is_data_only_repo", metrics.is_data_only_repo),
        ("code_health_is_flat_dump", metrics.is_flat_dump),
    ):
        _save(
            key, value, "bool",
            is_available=value is not None,
            reason="" if value is not None else "нет данных (дерево не получено)",
        )

    _save(
        "code_health_source_files_count", metrics.source_files_count, "files",
        is_available=metrics.source_files_count is not None,
        source_reference=repo_url if metrics.source_files_count is not None else "",
    )
    _save(
        "code_health_total_files_count", metrics.total_files_count, "files",
        is_available=metrics.total_files_count is not None,
        source_reference=repo_url if metrics.total_files_count is not None else "",
    )
    _save(
        "code_health_todo_scan_files_count", metrics.todo_scan_files_count, "files",
        is_available=True,
        source_reference=repo_url if metrics.todo_scan_files_count else "",
    )
    _save(
        "code_health_todo_total_count", metrics.todo_total_count, "comments",
        is_available=metrics.todo_total_count is not None,
        reason=metrics.todo_scan_error,
        source_reference=repo_url if metrics.todo_total_count is not None else "",
    )
    _save(
        "code_health_todo_old_count", metrics.todo_old_count, "comments",
        is_available=metrics.todo_age_available,
        reason=metrics.todo_age_error,
        source_reference=repo_url if metrics.todo_age_available else "",
    )


def _bump(severity: str, category_score: float | None) -> str:
    return bump_severity(
        severity, category_score, CODE_HEALTH_CATEGORY_SCORE_SEVERITY_BUMP_THRESHOLD
    )


def _build_findings(
    scan: Scan,
    metrics: _CodeHealthMetrics,
    category_score: float | None,
    submetric_scores: dict[str, float] | None = None,
    include_todo_age: bool = True,
) -> None:
    Finding.objects.filter(scan=scan, category=CATEGORY).delete()

    findings: list[Finding] = []
    submetric_scores = submetric_scores or {}
    weights = _weights_for(include_todo_age)

    if metrics.tests_present is False:
        findings.append(Finding(
            scan=scan, category=CATEGORY,
            severity=_bump(Finding.Severity.HIGH, category_score),
            title="Не найдено тестов",
            detail="В дереве репозитория не обнаружено ни каталогов tests/test/spec, ни файлов с типовыми именами тестов.",
            recommendation="Добавьте тесты хотя бы для критичной части кодовой базы и подключите их к CI.",
            evidence_refs=["tests:not-found"],
            estimated_score_impact=find_impact(
                "tests_present", weights, submetric_scores
            ),
        ))

    if metrics.lint_config_present is False:
        findings.append(Finding(
            scan=scan, category=CATEGORY,
            severity=_bump(Finding.Severity.MEDIUM, category_score),
            title="Нет конфигурации линтера/форматтера",
            detail="В репозитории не найдено конфигов известных линтеров/форматтеров (ESLint, Ruff/Flake8, Prettier, rustfmt и т.п.).",
            recommendation="Подключите линтер и форматтер под используемый стек и зафиксируйте правила в конфиге.",
            evidence_refs=["lint-config:not-found"],
            estimated_score_impact=find_impact(
                "lint_config_present", weights, submetric_scores
            ),
        ))

    if metrics.is_data_only_repo:
        findings.append(Finding(
            scan=scan, category=CATEGORY,
            severity=_bump(Finding.Severity.MEDIUM, category_score),
            title="Репозиторий похож на набор данных, а не на код",
            detail="В дереве найдены только data-файлы (CSV/JSON/...) и ни одного файла исходного кода.",
            recommendation="Если это действительно data-репозиторий — уберите его из анализа кода; если код должен быть, проверьте, не потерялся ли он.",
            evidence_refs=["structure:data-only"],
            estimated_score_impact=find_impact(
                "structure", weights, submetric_scores
            ),
        ))
    elif metrics.is_flat_dump:
        findings.append(Finding(
            scan=scan, category=CATEGORY,
            severity=_bump(Finding.Severity.LOW, category_score),
            title="Файлы свалены в корень без структуры",
            detail="Большая часть файлов лежит прямо в корне репозитория без разбиения на каталоги.",
            recommendation="Разнесите код по каталогам по смыслу (например src/, tests/, docs/).",
            evidence_refs=["structure:flat-dump"],
            estimated_score_impact=find_impact(
                "structure", weights, submetric_scores
            ),
        ))

    if metrics.vendored_deps_present or metrics.generated_artifacts_present or metrics.binary_junk_present:
        junk_kinds = []
        if metrics.vendored_deps_present:
            junk_kinds.append("закоммиченные зависимости (например node_modules/vendor)")
        if metrics.generated_artifacts_present:
            junk_kinds.append("каталоги сборки (dist/build/target и т.п.)")
        if metrics.binary_junk_present:
            junk_kinds.append("скомпилированные/бинарные файлы")
        junk_refs = []
        if metrics.vendored_deps_present:
            junk_refs.append("junk:vendored-deps")
        if metrics.generated_artifacts_present:
            junk_refs.append("junk:generated-artifacts")
        if metrics.binary_junk_present:
            junk_refs.append("junk:binary-files")
        findings.append(Finding(
            scan=scan, category=CATEGORY,
            severity=_bump(Finding.Severity.MEDIUM, category_score),
            title="В репозитории закоммичен мусор",
            detail="Обнаружены: " + "; ".join(junk_kinds) + ".",
            recommendation="Добавьте эти пути в .gitignore и удалите их из истории репозитория.",
            evidence_refs=junk_refs,
            estimated_score_impact=find_impact(
                "no_committed_junk", weights, submetric_scores
            ),
        ))

    if metrics.dependency_manifest_present is False and metrics.source_files_count:
        findings.append(Finding(
            scan=scan, category=CATEGORY,
            severity=_bump(Finding.Severity.LOW, category_score),
            title="Не найден манифест зависимостей",
            detail="В репозитории есть исходный код, но не найдено ни одного из известных файлов манифеста зависимостей.",
            recommendation="Зафиксируйте зависимости явным манифестом (package.json/pyproject.toml/go.mod и т.п.).",
            evidence_refs=["manifest:not-found"],
            estimated_score_impact=find_impact(
                "dependency_hygiene", weights, submetric_scores
            ),
        ))
    elif metrics.dependency_manifest_present and metrics.lockfile_present is False:
        findings.append(Finding(
            scan=scan, category=CATEGORY,
            severity=_bump(Finding.Severity.LOW, category_score),
            title="Нет lockfile",
            detail="Манифест зависимостей есть, но lockfile не найден — версии зависимостей не зафиксированы.",
            recommendation="Добавьте и закоммитьте lockfile (package-lock.json/poetry.lock/go.sum и т.п.) для воспроизводимых сборок.",
            evidence_refs=["lockfile:not-found"],
            estimated_score_impact=find_impact(
                "dependency_hygiene", weights, submetric_scores
            ),
        ))

    if metrics.todo_total_count:
        if metrics.todo_age_available and metrics.todo_old_count:
            detail = (
                f"Найдено {metrics.todo_total_count} TODO/FIXME/HACK/XXX "
                f"(в {metrics.todo_scan_files_count} проверенных файлах), "
                f"из них {metrics.todo_old_count} старше "
                f"{CODE_HEALTH_TODO_OLD_AGE_DAYS} дней."
            )
        else:
            detail = (
                f"Найдено {metrics.todo_total_count} TODO/FIXME/HACK/XXX "
                f"(в {metrics.todo_scan_files_count} проверенных файлах)"
                + ("; давность не определена." if not metrics.todo_age_available else ".")
            )
        findings.append(Finding(
            scan=scan, category=CATEGORY,
            severity=_bump(Finding.Severity.LOW, category_score),
            title="Накопленные TODO/FIXME",
            detail=detail,
            recommendation="Проревизируйте старые TODO/FIXME: часть закройте, часть переведите в issues с владельцем.",
            evidence_refs=[f"todos:{metrics.todo_total_count}"],
            estimated_score_impact=find_impact(
                "todo_debt", weights, submetric_scores
            ),
        ))

    if metrics.todo_scan_error:
        findings.append(Finding(
            scan=scan, category=CATEGORY,
            severity=Finding.Severity.LOW,
            title="Не удалось просканировать файлы на TODO/FIXME",
            detail=f"Ошибка: {metrics.todo_scan_error}",
            recommendation="Проверьте доступность файлового API SourceCraft.",
            evidence_refs=["todos:scan-error"],
            estimated_score_impact=0,
        ))

    if (
        metrics.tests_present is None
        and metrics.lint_config_present is None
        and metrics.dependency_manifest_present is None
    ):
        findings.append(Finding(
            scan=scan, category=CATEGORY,
            severity=Finding.Severity.LOW,
            title="Недостаточно данных для оценки состояния кода",
            detail="Не удалось получить дерево файлов репозитория — ни одна метрика категории не рассчитана.",
            recommendation="Проверьте доступность SourceCraft API.",
            evidence_refs=[],
            estimated_score_impact=0,
        ))

    if findings:
        Finding.objects.bulk_create(findings)


def run_code_health_scan(
    scan: Scan,
    client: SourceCraftClient,
    git_client: SourceCraftGitClient | None,
    include_todo_age: bool,
    file_client: SourceCraftFileClient,
) -> HealthScore:
    repository = scan.repository

    try:
        tree_list = get_repository_tree_cached(client, repository)
    except SourceCraftError as exc:
        logger.error(f"Не удалось получить дерево файлов {repository}: {exc}")
        with transaction.atomic():
            MetricSample.objects.update_or_create(
                scan=scan,
                category=CATEGORY,
                metric_key="code_health_fetch_error",
                defaults=dict(
                    value=None,
                    is_available=False,
                    error_reason="не удалось получить дерево файлов",
                    source_reference="",
                ),
            )
            health_score, _ = HealthScore.objects.update_or_create(
                scan=scan,
                category=CATEGORY,
                defaults=dict(
                    total=None,
                    weight_used=CATEGORY_WEIGHT,
                    data_completeness=0.0,
                    raw_metrics={},
                ),
            )
        return health_score

    tree: dict[str, dict[str, Any]] = {}
    for entry in tree_list:
        path = str(entry.get("path") or entry.get("name") or "")
        if path:
            tree[path.lower()] = entry

    repository.scan_commit_sha = scan.commit_sha_at_analysis

    metrics = _compute_metrics(
        git_client, repository, tree, include_todo_age, scan.id, file_client
    )

    score, data_completeness, submetric_scores = score_code_health_category(
        metrics, include_todo_age=include_todo_age
    )

    with transaction.atomic():
        _save_metric_samples(scan, metrics)
        _build_findings(
            scan, metrics, score, submetric_scores,
            include_todo_age=include_todo_age,
        )

        health_score, _ = HealthScore.objects.update_or_create(
            scan=scan,
            category=CATEGORY,
            defaults=dict(
                total=score,
                weight_used=CATEGORY_WEIGHT,
                data_completeness=data_completeness,
                raw_metrics={
                    "dependency_manifest_present": metrics.dependency_manifest_present,
                    "lockfile_present": metrics.lockfile_present,
                    "tests_present": metrics.tests_present,
                    "lint_config_present": metrics.lint_config_present,
                    "vendored_deps_present": metrics.vendored_deps_present,
                    "generated_artifacts_present": metrics.generated_artifacts_present,
                    "binary_junk_present": metrics.binary_junk_present,
                    "is_data_only_repo": metrics.is_data_only_repo,
                    "is_flat_dump": metrics.is_flat_dump,
                    "source_files_count": metrics.source_files_count,
                    "total_files_count": metrics.total_files_count,
                    "clone_available": metrics.clone_available,
                    "todo_total_count": metrics.todo_total_count,
                    "todo_old_count": metrics.todo_old_count,
                    "todo_age_available": metrics.todo_age_available,
                    "include_todo_age": include_todo_age,
                    "todo_scan_files_count": metrics.todo_scan_files_count,
                    "fetch_errors": metrics.fetch_errors,
                    "submetric_scores": submetric_scores,
                },
            ),
        )
    return health_score


def run(scan_id: int) -> int:
    scan = Scan.objects.select_related(
        "triggered_by_user", "repository"
    ).get(pk=scan_id)

    include_todo_age = False

    token = None
    if scan.triggered_by_user_id:
        token = scan.triggered_by_user.profile.sourcecraft_token
        include_todo_age = True

    client = SourceCraftClient(token=token)
    file_client = SourceCraftFileClient(token=token)

    git_client = None
    if include_todo_age and token:
        git_client = SourceCraftGitClient(token=token)

    health_score = run_code_health_scan(
        scan, client, git_client, include_todo_age, file_client
    )
    return health_score.pk
