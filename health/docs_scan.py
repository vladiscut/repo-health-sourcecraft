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

from health.models import (
    Finding,
    HealthScore,
    MetricSample,
    Scan,
    Repository,
)
from health.scoring import (
    CATEGORY_WEIGHTS,
    DOCS_CATEGORY_SCORE_SEVERITY_BUMP_THRESHOLD,
    bump_severity,
    score_docs_category,
)
from integrations.sourcecraft import (
    SourceCraftClient,
    SourceCraftFileClient,
    SourceCraftError,
)


logger = logging.getLogger(__name__)

CATEGORY = MetricSample.Category.DOCS

# Номинальный вес категории по ТЗ
# Финальная перенормировка между всеми 6 категориями — задача
# health.orchestrator.aggregate_scan.
CATEGORY_WEIGHT = CATEGORY_WEIGHTS[CATEGORY]

README_MAX_CHARS = 200_000

# Конвенция SourceCraft для CI: каталог `.sourcecraft/` с файлом вида
# ci.yaml/ci.yml внутри (см. пример дерева репозитория на платформе).
# Это ОСНОВНАЯ конвенция для проектов SourceCraft — импорт с внешних
# платформ не требуется по ТЗ, поэтому её приоритизируем, но
# GitHub/GitLab-конвенции оставляем как доп. сигналы на случай, если
# конфиг остался в истории репозитория.
SOURCECRAFT_CI_DIR = ".sourcecraft"
SOURCECRAFT_CI_FILENAME_STEMS = ("ci",)

# Ключевые слова/заголовки, указывающие на инструкцию локального запуска.
# команды популярных экосистем (pip/npm/yarn/pnpm/poetry/go/cargo/make)
# и упоминания docker/compose
LOCAL_RUN_PATTERNS = [
    r"quick\s*start",
    r"getting\s*started",
    r"get\s+started",
    r"how\s+to\s+(run|start|launch)",
    r"\binstallation\b",
    r"\binstall(ing)?\b",
    r"\bsetup\b",
    r"\bset[\s-]*up\b",
    r"local\s+(development|dev|run|setup|environment)",
    r"running\s+(the\s+)?(project|app|application|service|server)",
    r"run\s+locally",
    r"prerequisites",
    r"requirements",
    r"environment\s+variables",
    r"\.env\b",
    r"pip\s+install",
    r"pipx\s+install",
    r"poetry\s+(install|run)",
    r"pipenv\s+install",
    r"conda\s+(install|create)",
    r"npm\s+(install|ci|start|run)",
    r"yarn\s+(install|start)",
    r"pnpm\s+(install|start)",
    r"go\s+run",
    r"go\s+install",
    r"cargo\s+run",
    r"cargo\s+install",
    r"bundle\s+install",
    r"composer\s+install",
    r"docker\s+run",
    r"docker-compose",
    r"docker\s+compose",
    r"make\s+(run|start|up|install)",
    r"\bvenv\b",
    r"virtualenv",
    r"python\s+manage\.py\s+runserver",
    # Русскоязычные варианты
    r"быстрый\s+старт",
    r"начало\s+работы",
    r"как\s+запустить",
    r"локальн(ый|ая|ое)\s+(запуск|разработк|окружени|развёртывани|развертывани)",
    r"запуск\s+(проекта|приложения|сервиса)",
    r"установка\s+и\s+запуск",
    r"\bустановка\b",
    r"предварительные\s+требования",
    r"зависимост",
    r"переменные\s+окружения",
    r"первый\s+запуск",
    r"инструкция\s+по\s+запуску",
]

# Ключевые слова/заголовки, указывающие на инструкции по сборке и тестам.
BUILD_TEST_PATTERNS = [
    r"\bbuild(ing)?\b",
    r"\bcompil(e|ing|ation)\b",
    r"\btest(s|ing)?\b",
    r"unit\s+tests?",
    r"integration\s+tests?",
    r"end[\s-]*to[\s-]*end\s+tests?",
    r"\be2e\b",
    r"test\s+coverage",
    r"code\s+coverage",
    r"continuous\s+integration",
    r"\blint(ing)?\b",
    r"pytest",
    r"unittest",
    r"tox\b",
    r"nox\b",
    r"jest\b",
    r"mocha\b",
    r"vitest\b",
    r"cypress\b",
    r"playwright\b",
    r"junit\b",
    r"go\s+test",
    r"cargo\s+test",
    r"make\s+(build|test|check|lint)",
    r"npm\s+(run\s+)?(build|test)",
    r"yarn\s+(build|test)",
    r"tox\s+-e",
    r"\bci/cd\b",
    r"github\s+actions",
    r"gitlab[\s-]*ci",
    r"pre-commit",
    # Русскоязычные варианты
    r"сборк[а-я]*",
    r"тестировани[а-я]*",
    r"\bтест[а-я]*\b",
    r"модульные?\s+тест",
    r"интеграционные?\s+тест",
    r"покрытие\s+тест",
    r"запуск\s+тест",
    r"как\s+собрать",
    r"как\s+протестировать",
    r"проверка\s+кода",
]

# Ключевые слова/заголовки, указывающие на описание структуры проекта.
STRUCTURE_PATTERNS = [
    r"структур[аеу]",
    r"project\s+structure",
    r"repository\s+structure",
    r"repo\s+structure",
    r"directory\s+structure",
    r"folder\s+structure",
    r"file\s+structure",
    r"\blayout\b",
    r"архитектур[а-я]*",
    r"\barchitecture\b",
    r"code\s+organization",
    r"codebase\s+overview",
    r"module\s+overview",
    r"components?\s+overview",
    r"directory\s+(tree|layout)",
    r"project\s+layout",
    r"what'?s\s+in\s+this\s+repo",
    r"folder\s+organization",
    r"repository\s+layout",
    # Русскоязычные варианты
    r"структура\s+(проекта|репозитория|каталогов|папок)",
    r"организация\s+(кода|проекта)",
    r"устройство\s+проекта",
    r"описание\s+(модулей|компонентов)",
    r"дерево\s+(каталогов|директорий)",
]

# Распознавание типа лицензии по содержимому файла LICENSE.
LICENSE_PATTERNS = [
    ("MIT", r"MIT\s+License"),
    ("Apache-2.0", r"Apache\s+License,?\s+Version\s+2\.0"),
    ("Apache-2.0", r"\bApache-2\.0\b"),
    ("GPL-3.0", r"GNU\s+GENERAL\s+PUBLIC\s+LICENSE\s*[,\s]*Version\s+3"),
    ("GPL-2.0", r"GNU\s+GENERAL\s+PUBLIC\s+LICENSE\s*[,\s]*Version\s+2"),
    ("GPL", r"GNU\s+General\s+Public\s+License"),
    ("LGPL", r"GNU\s+Lesser\s+General\s+Public\s+License"),
    ("LGPL", r"\bLGPL\b"),
    ("AGPL", r"GNU\s+Affero\s+General\s+Public\s+License"),
    ("AGPL", r"\bAGPL\b"),
    ("BSD-3-Clause", r"BSD\s+3-Clause\s+License"),
    ("BSD-2-Clause", r"BSD\s+2-Clause\s+License"),
    ("BSD", r"BSD\s+\d?-?Clause|BSD\s+License"),
    ("MPL-2.0", r"Mozilla\s+Public\s+License,?\s+version\s+2\.0"),
    ("MPL", r"Mozilla\s+Public\s+License"),
    ("ISC", r"\bISC\s+License\b"),
    ("Unlicense", r"\bThis\s+is\s+free\s+and\s+unencumbered\s+software\b"),
    ("Unlicense", r"^\s*Unlicense\s*$"),
    ("CC0", r"CC0\s+1\.0\s+Universal"),
    ("CC-BY", r"Creative\s+Commons\s+Attribution"),
    ("EPL", r"Eclipse\s+Public\s+License"),
    ("WTFPL", r"DO\s+WHAT\s+THE\s+F\*?CK\s+YOU\s+WANT"),
    ("Boost", r"Boost\s+Software\s+License"),
    ("Zlib", r"zlib\s+License"),
    ("Artistic-2.0", r"Artistic\s+License\s+2\.0"),
    ("Proprietary", r"[Aa]ll\s+[Rr]ights\s+[Rr]eserved"),
    # Русскоязычные варианты
    ("Proprietary", r"[Вв]се\s+права\s+защищены"),
]

# Стемы имён файлов (без расширения, без учёта регистра), которые ищем
# не только в корне, но и в типовых подпапках (.github/, docs/, .gitlab/)
CONTRIBUTING_STEMS = (
    "contributing",
    "contribute",
    "contributors",
    "contribution",
    "contribution_guide",
    "contribution-guide",
    "contributing_guide",
    "contributing-guide",
    "how_to_contribute",
    "how-to-contribute",
)


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


def _has_sourcecraft_ci_config(tree: dict[str, dict[str, Any]]) -> bool:
    """Ищет CI-конфиг SourceCraft: файл `ci.<ext>` внутри `.sourcecraft/`.

    Не привязываемся к конкретному расширению (.yaml/.yml/.json) —
    проверяем стем имени файла.
    """

    prefix = SOURCECRAFT_CI_DIR + "/"
    for path_lower, entry in tree.items():
        if not path_lower.startswith(prefix):
            continue
        entry_type = str(entry.get("type") or "").lower()
        if entry_type not in ("file", "executable"):
            continue
        name = path_lower[len(prefix):]
        if "/" in name:
            continue  # только файлы прямо в .sourcecraft/, не глубже
        stem = name.rsplit(".", 1)[0] if "." in name else name
        if stem in SOURCECRAFT_CI_FILENAME_STEMS:
            return True
    return False


def _detect_ci_config_present(tree: dict[str, dict[str, Any]]) -> bool:
    """Определяет наличие CI-конфига по всем известным конвенциям.

    Порядок проверки: сначала родная конвенция SourceCraft
    (`.sourcecraft/ci.yaml`), затем GitHub Actions / GitLab CI — на
    случай, если в репозитории остались файлы истории миграции с
    другой платформы, затем — legacy-эвристика по имени файла
    `sourcecraft-ci*`
    """

    if _has_sourcecraft_ci_config(tree):
        return True

    github_gitlab_candidates = (
        ".gitlab-ci.yml",
        ".github/workflows",
    )
    if _has_path(tree, github_gitlab_candidates[0]):
        return True
    prefix = github_gitlab_candidates[1]
    if any(p == prefix or p.startswith(prefix + "/") for p in tree):
        return True

    for path_lower in tree:
        name = path_lower.rsplit("/", 1)[-1]
        if name.startswith("sourcecraft-ci") or name.startswith(".sourcecraft-ci"):
            return True

    return False


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
    file_client: SourceCraftFileClient,
    repo: Repository,
    path: str
) -> tuple[str | None, str]:
    if not repo.scan_commit_sha:
        return None, "нет хеша последнего коммита"
    try:
        content = file_client.get_file_text(
            repo.org_slug, repo.repo_slug, path, repo.scan_commit_sha
        )
    except SourceCraftError as exc:
        if exc.status_code == 404:
            return None, "файл не найден (404)"
        return None, f"ошибка API: {exc}"

    if len(content) > README_MAX_CHARS:
        content = content[:README_MAX_CHARS]
    return content, ""


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
    """Ищет файл по имени без расширения в любом месте
    дерева — используется для файлов вроде CONTRIBUTING, которые по
    конвенции нередко лежат в .github/ или docs/
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
    file_client: SourceCraftFileClient,
    repo,
    tree: dict[str, dict[str, Any]],
) -> _DocsMetrics:
    metrics = _DocsMetrics()

    # README
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

    # LICENSE
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

    # Документы и шаблоны по дереву
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

    metrics.ci_config_present = _detect_ci_config_present(tree)

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
    file_client: SourceCraftFileClient,
) -> HealthScore:
    """Собирает данные по документации репозитория и сохраняет результат."""

    repository = scan.repository

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

    repository.scan_commit_sha = scan.commit_sha_at_analysis

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
    file_client = SourceCraftFileClient()
    scan = Scan.objects.select_related("repository").get(pk=scan_id)
    health_score = run_docs_scan(scan, client, file_client)
    return health_score.pk
