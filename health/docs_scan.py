"""Прогон анализа категории "Documentation & Best Practices" для Scan.

Модуль сфокусирован только на категории Docs — он не трогает
Scan.status итогового скана и не пересчитывает общий Repo Health Score.

Он отвечает за:

1. Получение дерева файлов репозитория через SourceCraftClient.
2. Чтение содержимого ключевых файлов (README, LICENSE) по прямому URL
   репозитория (в SourceCraftClient нет метода чтения файла).
3. Вычисление сырых метрик категории Docs и запись их в MetricSample.
4. Расчёт балла категории 0-100 (или None при отсутствии данных) —
   делегирован в health.scoring.score_docs_category, здесь модуль только
   собирает _DocsMetrics и не занимается арифметикой скоринга.
5. Формирование приоритизированных Finding по обнаруженным проблемам.
"""

import logging
import re

from dataclasses import dataclass
from typing import Any

from django.db import transaction

from health.models import Finding, HealthScore, MetricSample, Scan
from health.scoring import (
    CATEGORY_WEIGHTS,
    DOCS_CATEGORY_SCORE_SEVERITY_BUMP_THRESHOLD,
    bump_severity,
    score_docs_category,
)
from integrations.sourcecraft import SourceCraftClient, SourceCraftError


logger = logging.getLogger(__name__)

CATEGORY = MetricSample.Category.DOCS

# Номинальный вес категории по ТЗ — единственный источник: health.scoring.
# Финальная перенормировка между всеми 6 категориями — задача
# health.orchestrator.aggregate_scan.
CATEGORY_WEIGHT = CATEGORY_WEIGHTS[CATEGORY]

README_MAX_CHARS = 200_000

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".pyc",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".avi", ".mov", ".webm",
}

LOCAL_RUN_PATTERNS = [
    r"quick\s*start", r"getting\s*started", r"installation", r"install",
    r"запуск", r"установка", r"pip\s+install", r"npm\s+install",
    r"yarn\s+install", r"docker\s+run", r"docker-compose",
]
BUILD_TEST_PATTERNS = [
    r"\bbuild\b", r"\btest", r"сборка", r"тестирование", r"тесты",
    r"pytest", r"make\s+test", r"npm\s+test", r"tox",
]
STRUCTURE_PATTERNS = [
    r"структур[аеу]", r"project\s+structure", r"repository\s+structure",
    r"directory\s+structure", r"layout", r"архитектур",
]
LICENSE_PATTERNS = [
    ("MIT", r"MIT\s+License"),
    ("Apache-2.0", r"Apache\s+License,?\s+Version\s+2\.0"),
    ("GPL", r"GNU\s+General\s+Public\s+License"),
    ("BSD", r"BSD\s+\d?-?Clause|BSD\s+License"),
]

# Стемы имён файлов (без расширения, без учёта регистра), которые ищем
# не только в корне, но и в типовых подпапках (.github/, docs/) —
# CONTRIBUTING по конвенции часто лежит именно там.
CONTRIBUTING_STEMS = ("contributing",)


@dataclass
class _DocsMetrics:
    """Промежуточный результат вычислений — перед сохранением в БД."""

    readme_present: bool | None = None
    readme_path: str | None = None
    readme_size_chars: int | None = None
    readme_has_local_run: bool | None = None
    readme_has_build_test: bool | None = None
    readme_has_structure: bool | None = None
    readme_read_error: str = ""

    license_present: bool | None = None
    license_path: str | None = None
    license_type_recognized: bool | None = None
    license_read_error: str = ""

    contributing_present: bool | None = None
    codeowners_present: bool | None = None
    changelog_present: bool | None = None
    docs_dir_present: bool | None = None
    issue_templates_present: bool | None = None
    pr_template_present: bool | None = None
    ci_config_present: bool | None = None


def _collect_tree(
    client: SourceCraftClient,
    repo,
) -> dict[str, dict[str, Any]] | None:
    """Возвращает нормализованное дерево репозитория или None при ошибке.

    Ключ — путь в нижнем регистре, значение — исходная запись дерева
    (name, path, type).
    """

    try:
        tree = client.get_repository_file_tree(
            repo.sourcecraft_id, repo.default_branch or None
        )
    except SourceCraftError as exc:
        logger.error(f"Не удалось получить дерево файлов {repo}: {exc}")
        return None

    normalized: dict[str, dict[str, Any]] = {}
    for entry in tree:
        path = str(entry.get("path") or entry.get("name") or "")
        if not path:
            continue
        normalized[path.lower()] = entry
    return normalized


def _read_file_safe(
    file_client: None, repo, path: str
) -> tuple[str | None, str]:
    """Читает содержимое файла через SourceCraftFileClient.

    Возвращает ``(content_or_None, error_reason)``. Никогда не бросает.
    """

    content, reason = file_client.get_text(repo.url, path)
    if content is not None and len(content) > README_MAX_CHARS:
        content = content[:README_MAX_CHARS]
    return content, reason


def _matches_any(text: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return True
    return False


def _find_root_file(
    tree: dict[str, dict[str, Any]],
    prefixes: tuple[str, ...],
    suffixes: tuple[str, ...] = (),
    types: tuple[str, ...] = ("file", "executable"),
) -> str | None:
    """Ищет корневой файл по префиксу/суффиксу в нормализованном дереве."""

    for path_lower, entry in tree.items():
        if "/" in path_lower:
            continue
        entry_type = str(entry.get("type") or "").lower()
        if types and entry_type not in types:
            continue
        name_lower = path_lower
        if any(name_lower.startswith(p) for p in prefixes):
            return str(entry.get("path") or entry.get("name"))
        if suffixes and any(name_lower.endswith(s) for s in suffixes):
            return str(entry.get("path") or entry.get("name"))
    return None


def _find_file_by_stem(
    tree: dict[str, dict[str, Any]],
    stems: tuple[str, ...],
    types: tuple[str, ...] = ("file", "executable"),
) -> str | None:
    """Ищет файл по имени без расширения в любом месте дерева (не только в
    корне) — используется для файлов вроде CONTRIBUTING, которые по
    конвенции нередко лежат в .github/ или docs/, а не только в корне.
    """

    for path_lower, entry in tree.items():
        entry_type = str(entry.get("type") or "").lower()
        if types and entry_type not in types:
            continue
        name = path_lower.rsplit("/", 1)[-1]
        stem = name.rsplit(".", 1)[0] if "." in name else name
        if stem in stems:
            return str(entry.get("path") or entry.get("name"))
    return None


def _has_path(tree: dict[str, dict[str, Any]], path_lower: str) -> bool:
    return path_lower.lower() in tree


def _has_dir(tree: dict[str, dict[str, Any]], dir_lower: str) -> bool:
    entry = tree.get(dir_lower)
    if entry is None:
        return False
    return str(entry.get("type") or "").lower() == "dir"


def _has_prefix_in(
    tree: dict[str, dict[str, Any]],
    parent_lower: str,
    prefixes: tuple[str, ...],
) -> bool:
    prefix = parent_lower.rstrip("/") + "/"
    for path_lower in tree:
        if not path_lower.startswith(prefix):
            continue
        tail = path_lower[len(prefix):]
        if any(tail.startswith(p) for p in prefixes):
            return True
    return False


def _compute_metrics(
    file_client: None,
    repo,
    tree: dict[str, dict[str, Any]],
) -> _DocsMetrics:
    metrics = _DocsMetrics()

    # README -----------------------------------------------------------
    readme_path = _find_root_file(tree, ("readme",))
    metrics.readme_present = readme_path is not None
    metrics.readme_path = readme_path
    if readme_path:
        content, reason = _read_file_safe(file_client, repo, readme_path)
        if content is None:
            metrics.readme_read_error = reason
            logger.info(f"README {repo} не прочитан: {reason}")
        else:
            metrics.readme_size_chars = len(content)
            metrics.readme_has_local_run = _matches_any(content, LOCAL_RUN_PATTERNS)
            metrics.readme_has_build_test = _matches_any(content, BUILD_TEST_PATTERNS)
            metrics.readme_has_structure = _matches_any(content, STRUCTURE_PATTERNS)

    # LICENSE ----------------------------------------------------------
    license_path = _find_root_file(
        tree, ("license", "copying", "licence"), suffixes=(".md", ".txt")
    )
    if license_path is None:
        license_path = _find_root_file(tree, ("license", "copying", "licence"))
    metrics.license_present = license_path is not None
    metrics.license_path = license_path
    if license_path:
        content, reason = _read_file_safe(file_client, repo, license_path)
        if content is None:
            metrics.license_read_error = reason
            logger.info(f"LICENSE {repo} не прочитан: {reason}")
        else:
            recognized = any(
                re.search(pattern, content, flags=re.IGNORECASE)
                for _name, pattern in LICENSE_PATTERNS
            )
            metrics.license_type_recognized = recognized

    # Документы и шаблоны по дереву -------------------------------------
    # CONTRIBUTING ищем не только в корне, но и в .github/, docs/ —
    # по конвенции он нередко лежит именно там.
    metrics.contributing_present = _find_file_by_stem(
        tree, CONTRIBUTING_STEMS
    ) is not None

    metrics.changelog_present = _find_root_file(
        tree, ("changelog", "changes", "history")
    ) is not None

    metrics.docs_dir_present = _has_dir(tree, "docs")

    codeowners_paths = (
        "codeowners",
        ".github/codeowners",
        "docs/codeowners",
    )
    metrics.codeowners_present = any(
        _has_path(tree, p) for p in codeowners_paths
    )

    metrics.issue_templates_present = _has_prefix_in(
        tree, ".github/issue_template", ("",)
    )

    pr_template_prefixes = (
        "pull_request_template",
    )
    metrics.pr_template_present = (
        _has_prefix_in(tree, ".github", pr_template_prefixes)
        or _has_prefix_in(tree, "docs", pr_template_prefixes)
    )

    ci_candidates = (
        ".gitlab-ci.yml",
        ".github/workflows",
    )
    ci_present = _has_path(tree, ci_candidates[0]) or any(
        p == ci_candidates[1] or p.startswith(ci_candidates[1] + "/")
        for p in tree
    )
    if not ci_present:
        for path_lower in tree:
            name = path_lower.rsplit("/", 1)[-1]
            if name.startswith("sourcecraft-ci") or name.startswith(".sourcecraft-ci"):
                ci_present = True
                break
    metrics.ci_config_present = ci_present

    return metrics


def _save_metric_samples(scan: Scan, metrics: _DocsMetrics) -> None:
    def _save(
        key: str,
        value: Any,
        unit: str = "",
        is_available: bool = True,
        reason: str = "",
    ) -> None:
        MetricSample.objects.update_or_create(
            scan=scan,
            category=CATEGORY,
            metric_key=key,
            defaults=dict(
                value=value,
                unit=unit,
                is_available=is_available,
                error_reason=reason,
            ),
        )

    _save(
        "docs_readme_present",
        metrics.readme_present,
        "bool",
        is_available=metrics.readme_present is not None,
        reason="" if metrics.readme_present is not None else "нет данных",
    )
    _save(
        "docs_readme_size_chars",
        metrics.readme_size_chars,
        "chars",
        is_available=metrics.readme_size_chars is not None,
        reason=(
            ""
            if metrics.readme_size_chars is not None
            else (metrics.readme_read_error or "README не прочитан")
        ),
    )
    for key, value in (
        ("docs_readme_has_local_run", metrics.readme_has_local_run),
        ("docs_readme_has_build_test", metrics.readme_has_build_test),
        ("docs_readme_has_structure", metrics.readme_has_structure),
    ):
        _save(
            key,
            value,
            "bool",
            is_available=value is not None,
            reason=(
                ""
                if value is not None
                else (metrics.readme_read_error or "нет данных")
            ),
        )

    _save(
        "docs_license_type_recognized",
        metrics.license_type_recognized,
        "bool",
        is_available=metrics.license_type_recognized is not None,
        reason=(
            ""
            if metrics.license_type_recognized is not None
            else (metrics.license_read_error or "нет данных")
        ),
    )

    for key, value in (
        ("docs_license_present", metrics.license_present),
        ("docs_contributing_present", metrics.contributing_present),
        ("docs_codeowners_present", metrics.codeowners_present),
        ("docs_changelog_present", metrics.changelog_present),
        ("docs_dir_present", metrics.docs_dir_present),
        ("docs_issue_templates_present", metrics.issue_templates_present),
        ("docs_pr_template_present", metrics.pr_template_present),
        ("docs_ci_config_present", metrics.ci_config_present),
    ):
        _save(key, value, "bool")


def _bump(severity: str, category_score: float | None) -> str:
    return bump_severity(
        severity, category_score, DOCS_CATEGORY_SCORE_SEVERITY_BUMP_THRESHOLD
    )


def _build_findings(
    scan: Scan,
    metrics: _DocsMetrics,
    category_score: float | None,
) -> None:
    Finding.objects.filter(scan=scan, category=CATEGORY).delete()

    findings: list[Finding] = []
    readme_refs = [metrics.readme_path] if metrics.readme_path else []
    license_refs = [metrics.license_path] if metrics.license_path else []

    if metrics.readme_present is False:
        findings.append(
            Finding(
                scan=scan,
                category=CATEGORY,
                severity=_bump(Finding.Severity.HIGH, category_score),
                title="Отсутствует README",
                detail=(
                    "В корне репозитория не найден файл README. Это "
                    "первое, что видит новый пользователь проекта."
                ),
                recommendation=(
                    "Добавьте README.md с описанием проекта, быстрым стартом, "
                    "инструкциями сборки и тестов."
                ),
                evidence_refs=[],
                estimated_score_impact=10,
            )
        )
    elif metrics.readme_size_chars is None:
        # README есть, но прочитать его не удалось — это отдельная от
        # "README отсутствует" ситуация, и её тоже нужно объяснить
        # пользователю, а не молча занижать/задирать оценку.
        findings.append(
            Finding(
                scan=scan,
                category=CATEGORY,
                severity=_bump(Finding.Severity.LOW, category_score),
                title="Не удалось прочитать содержимое README",
                detail=(
                    f"README найден по пути {metrics.readme_path}, но получить "
                    f"его содержимое не удалось "
                    f"({metrics.readme_read_error or 'неизвестная ошибка'})."
                ),
                recommendation=(
                    "Проверьте доступность файла по прямой ссылке и "
                    "корректность кодировки."
                ),
                evidence_refs=readme_refs,
                estimated_score_impact=0,
            )
        )
    else:
        if metrics.readme_has_local_run is False:
            findings.append(
                Finding(
                    scan=scan,
                    category=CATEGORY,
                    severity=_bump(Finding.Severity.MEDIUM, category_score),
                    title="Нет инструкции локального запуска",
                    detail=(
                        "В README нет явного блока про установку и запуск "
                        "проекта локально."
                    ),
                    recommendation=(
                        "Добавьте раздел Quick Start: зависимости, установка, "
                        "запуск, типовые команды."
                    ),
                    evidence_refs=readme_refs,
                    estimated_score_impact=6,
                )
            )

        if metrics.readme_has_build_test is False:
            findings.append(
                Finding(
                    scan=scan,
                    category=CATEGORY,
                    severity=_bump(Finding.Severity.MEDIUM, category_score),
                    title="Нет инструкций по сборке и тестам",
                    detail=(
                        "В README не описано, как собрать проект и запустить "
                        "тесты."
                    ),
                    recommendation=(
                        "Добавьте разделы Build и Test с командами и "
                        "необходимыми зависимостями."
                    ),
                    evidence_refs=readme_refs,
                    estimated_score_impact=4,
                )
            )

    if metrics.license_present is False:
        findings.append(
            Finding(
                scan=scan,
                category=CATEGORY,
                severity=_bump(Finding.Severity.MEDIUM, category_score),
                title="Отсутствует лицензия",
                detail=(
                    "В корне репозитория нет файла LICENSE/COPYING. Без "
                    "лицензии использование проекта юридически неясно."
                ),
                recommendation=(
                    "Добавьте LICENSE с выбранной лицензией (MIT, "
                    "Apache-2.0, GPL и т.п.)."
                ),
                evidence_refs=[],
                estimated_score_impact=6,
            )
        )
    elif metrics.license_type_recognized is False:
        findings.append(
            Finding(
                scan=scan,
                category=CATEGORY,
                severity=_bump(Finding.Severity.LOW, category_score),
                title="Тип лицензии не распознан",
                detail=(
                    "Файл лицензии есть, но его тип не удалось "
                    "автоматически распознать (MIT/Apache-2.0/GPL/BSD)."
                ),
                recommendation=(
                    "Используйте стандартный текст лицензии без "
                    "существенных правок."
                ),
                evidence_refs=license_refs,
                estimated_score_impact=2,
            )
        )
    elif metrics.license_type_recognized is None:
        # Файл лицензии найден, но прочитать его не удалось.
        findings.append(
            Finding(
                scan=scan,
                category=CATEGORY,
                severity=_bump(Finding.Severity.LOW, category_score),
                title="Не удалось прочитать файл лицензии",
                detail=(
                    f"Файл лицензии найден по пути {metrics.license_path}, "
                    f"но получить его содержимое не удалось "
                    f"({metrics.license_read_error or 'неизвестная ошибка'})."
                ),
                recommendation="Проверьте доступность файла по прямой ссылке.",
                evidence_refs=license_refs,
                estimated_score_impact=0,
            )
        )

    if not metrics.contributing_present and not metrics.codeowners_present:
        findings.append(
            Finding(
                scan=scan,
                category=CATEGORY,
                severity=_bump(Finding.Severity.LOW, category_score),
                title="Нет CONTRIBUTING и CODEOWNERS",
                detail=(
                    "Не найдены файлы CONTRIBUTING и CODEOWNERS — "
                    "внешним контрибьюторам неясен процесс."
                ),
                recommendation=(
                    "Добавьте CONTRIBUTING.md с процессом PR и CODEOWNERS "
                    "для автоназначения ревьюеров."
                ),
                evidence_refs=[],
                estimated_score_impact=2,
            )
        )

    if not metrics.changelog_present and not metrics.docs_dir_present:
        findings.append(
            Finding(
                scan=scan,
                category=CATEGORY,
                severity=_bump(Finding.Severity.LOW, category_score),
                title="Нет CHANGELOG и каталога docs/",
                detail=(
                    "Не найдены CHANGELOG и каталог docs/ — история "
                    "изменений и подробная документация отсутствуют."
                ),
                recommendation=(
                    "Заведите CHANGELOG.md и каталог docs/ для "
                    "пользовательской и разработческой документации."
                ),
                evidence_refs=[],
                estimated_score_impact=2,
            )
        )

    if (
        metrics.readme_present is None
        and metrics.license_present is None
        and metrics.contributing_present is None
        and metrics.docs_dir_present is None
    ):
        findings.append(
            Finding(
                scan=scan,
                category=CATEGORY,
                severity=Finding.Severity.LOW,
                title="Недостаточно данных для оценки категории Docs",
                detail=(
                    "Не удалось получить дерево файлов репозитория — "
                    "ни одна метрика категории не рассчитана."
                ),
                recommendation="Проверьте доступность SourceCraft API.",
                evidence_refs=[],
                estimated_score_impact=0,
            )
        )

    if findings:
        Finding.objects.bulk_create(findings)


def run_docs_scan(
    scan: Scan,
    client: SourceCraftClient,
) -> HealthScore:
    """Собирает данные по документации репозитория и сохраняет результат."""

    repository = scan.repository
    file_client = None # TODO

    # --- Сетевая часть: без открытой транзакции ---
    tree = _collect_tree(client, repository)
    if tree is None:
        with transaction.atomic():
            MetricSample.objects.update_or_create(
                scan=scan,
                category=CATEGORY,
                metric_key="docs_fetch_error",
                defaults=dict(
                    value=None,
                    is_available=False,
                    error_reason="не удалось получить дерево файлов",
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

    metrics = _compute_metrics(file_client, repository, tree)

    # --- Чистая арифметика: без сети и без БД ---
    score, data_completeness, submetric_scores = score_docs_category(metrics)

    # --- Запись в БД — единственное место с открытой транзакцией ---
    with transaction.atomic():
        _save_metric_samples(scan, metrics)
        _build_findings(scan, metrics, score)

        health_score, _ = HealthScore.objects.update_or_create(
            scan=scan,
            category=CATEGORY,
            defaults=dict(
                total=score,
                weight_used=CATEGORY_WEIGHT,
                data_completeness=data_completeness,
                raw_metrics={
                    "readme_present": metrics.readme_present,
                    "readme_size_chars": metrics.readme_size_chars,
                    "readme_has_local_run": metrics.readme_has_local_run,
                    "readme_has_build_test": metrics.readme_has_build_test,
                    "readme_has_structure": metrics.readme_has_structure,
                    "readme_read_error": metrics.readme_read_error,
                    "license_present": metrics.license_present,
                    "license_type_recognized": metrics.license_type_recognized,
                    "license_read_error": metrics.license_read_error,
                    "contributing_present": metrics.contributing_present,
                    "codeowners_present": metrics.codeowners_present,
                    "changelog_present": metrics.changelog_present,
                    "docs_dir_present": metrics.docs_dir_present,
                    "issue_templates_present": metrics.issue_templates_present,
                    "pr_template_present": metrics.pr_template_present,
                    "ci_config_present": metrics.ci_config_present,
                    "submetric_scores": submetric_scores,
                },
            ),
        )
    return health_score


def run(scan_id: int) -> int:
    client = SourceCraftClient()
    file_client = None # TODO
    scan = Scan.objects.select_related("repository").get(pk=scan_id)
    health_score = run_docs_scan(scan, client, file_client)
    return health_score.pk
