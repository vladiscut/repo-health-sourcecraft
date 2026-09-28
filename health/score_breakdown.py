"""Разбивка балла категории для карточки."""

from health.models import MetricSample
from health.scoring import (
    ACTIVITY_SUBMETRIC_WEIGHTS_WITH_COMMITS,
    ACTIVITY_SUBMETRIC_WEIGHTS_WITHOUT_COMMITS,
    CATEGORY_LABELS,
    CICD_SUBMETRIC_WEIGHTS,
    CODE_HEALTH_SUBMETRIC_WEIGHTS,
    CODE_HEALTH_SUBMETRIC_WEIGHTS_WITHOUT_TODO_AGE,
    DOCS_SUBMETRIC_WEIGHTS,
    ISSUES_SUBMETRIC_WEIGHTS,
    SECURITY_DIRECT_CRITICAL_PENALTY,
    SECURITY_HIGH_PENALTY,
    SECURITY_SUBMETRIC_WEIGHTS,
    SECURITY_TRANSITIVE_CRITICAL_PENALTY,
)
from health.security_scan import plain_security_reason


SUBMETRIC_LABELS = {
    "readme_quality": "README",
    "license": "Лицензия",
    "local_run": "Локальный запуск",
    "build_test": "Сборка и тесты",
    "contributing_codeowners": "CONTRIBUTING и CODEOWNERS",
    "structure_extras": "Структура и шаблоны",
    "success_rate": "Успешные прогоны",
    "duration": "Длительность",
    "recent_activity": "Свежесть",
    "commits": "Коммиты",
    "contributors": "Контрибьюторы",
    "merge_requests": "Merge requests",
    "releases": "Релизы",
    "stale_ratio": "Зависшие issues",
    "first_response": "Первый ответ",
    "time_to_close": "Время закрытия",
    "close_rate_30d": "Закрытие за 30 дней",
    "tests_present": "Тесты",
    "no_committed_junk": "Чистота репозитория",
    "structure": "Структура кода",
    "lint_config_present": "Линтер",
    "dependency_hygiene": "Зависимости",
    "todo_debt": "TODO и FIXME",
    "ci_config_present": "Конфигурация CI",
    "direct": "Прямые критические",
    "transitive": "Транзитивные критические",
    "high": "Высокие",
}


def rows_for_scores(scores) -> list[dict]:
    """Шесть категорий в порядке карточки, с частями балла."""

    by_category = {row.category: row for row in scores}
    rows = []
    for key, label in CATEGORY_LABELS.items():
        score = by_category.get(key)
        raw = (score.raw_metrics or {}) if score is not None else {}
        total = score.total if score is not None else None
        rows.append(
            {
                "label": label,
                "value": total,
                "parts": explain_category(key, raw, total),
            }
        )
    return rows


def explain_category(category: str, raw: dict | None, total: int | None) -> list[dict]:
    """Части одной категории: подпись, вес среди учтённых, факт и балл 0–100."""

    raw = raw or {}
    submetrics = raw.get("submetric_scores") or {}
    if submetrics:
        return _submetric_parts(category, raw, submetrics)
    if total is None:
        reason = _reason(raw)
        if not reason:
            return []
        return [_part(reason)]
    if category == MetricSample.Category.CI_CD:
        return _cicd_without_runs(raw)
    if category == MetricSample.Category.SECURITY:
        return _security_parts(raw)
    return []


def _submetric_parts(category: str, raw: dict, submetrics: dict) -> list[dict]:
    weights = _weights(category, raw, submetrics)
    known = [key for key in weights if key in submetrics]
    extra = [key for key in submetrics if key not in weights]
    shares = _shares(known, weights)
    parts = []
    for key in [*known, *extra]:
        score = _round_score(submetrics.get(key))
        if score is None:
            continue
        parts.append(
            _part(
                SUBMETRIC_LABELS.get(key, key),
                score=score,
                share=shares.get(key),
                detail=_detail(category, key, raw),
            )
        )
    return parts


def _weights(category: str, raw: dict, submetrics: dict) -> dict[str, float]:
    if category == MetricSample.Category.DOCS:
        return DOCS_SUBMETRIC_WEIGHTS
    if category == MetricSample.Category.ISSUES:
        return ISSUES_SUBMETRIC_WEIGHTS
    if category == MetricSample.Category.CI_CD:
        return CICD_SUBMETRIC_WEIGHTS
    if category == MetricSample.Category.ACTIVITY:
        include_commits = raw.get("include_commits")
        if include_commits is None:
            include_commits = "commits" in submetrics
        if include_commits:
            return ACTIVITY_SUBMETRIC_WEIGHTS_WITH_COMMITS
        return ACTIVITY_SUBMETRIC_WEIGHTS_WITHOUT_COMMITS
    if category == MetricSample.Category.CODE_HEALTH:
        include_todo_age = raw.get("include_todo_age")
        if include_todo_age is None:
            include_todo_age = raw.get("todo_age_available")
        if include_todo_age is False:
            return CODE_HEALTH_SUBMETRIC_WEIGHTS_WITHOUT_TODO_AGE
        return CODE_HEALTH_SUBMETRIC_WEIGHTS
    if category == MetricSample.Category.SECURITY:
        return SECURITY_SUBMETRIC_WEIGHTS
    return {}


def _shares(keys: list[str], weights: dict[str, float]) -> dict[str, int]:
    if not keys:
        return {}
    portions = [float(weights.get(key) or 0.0) for key in keys]
    total = sum(portions)
    if total <= 0:
        return {key: 0 for key in keys}
    exact = [part / total * 100 for part in portions]
    floors = [int(value) for value in exact]
    leftover = 100 - sum(floors)
    order = sorted(
        range(len(keys)),
        key=lambda index: (exact[index] - floors[index], -index),
        reverse=True,
    )
    for index in order[:leftover]:
        floors[index] += 1
    return dict(zip(keys, floors))


def _cicd_without_runs(raw: dict) -> list[dict]:
    if raw.get("ci_config_present") is False:
        return [_part("Нет .sourcecraft/ci.yaml")]
    if raw.get("ci_config_present") is True:
        return [_part("Конфигурация есть, завершённых прогонов нет")]
    return []


def _security_parts(raw: dict) -> list[dict]:
    critical = _as_number(raw.get("open_critical_count"))
    high = _as_number(raw.get("open_high_count"))
    if critical is None and high is None:
        return []
    critical_count = int(critical or 0)
    high_count = int(high or 0)
    transitive = int(_as_number(raw.get("transitive_critical_count")) or 0)
    transitive = min(max(transitive, 0), critical_count)
    direct = critical_count - transitive
    parts = []
    if direct:
        parts.append(_part("Прямые критические", detail=f"{direct} × −{SECURITY_DIRECT_CRITICAL_PENALTY}"))
    if transitive:
        parts.append(
            _part(
                "Транзитивные критические",
                detail=f"{transitive} × −{SECURITY_TRANSITIVE_CRITICAL_PENALTY}",
            )
        )
    if high_count:
        parts.append(_part("Высокие", detail=f"{high_count} × −{SECURITY_HIGH_PENALTY}"))
    if not parts:
        return [_part("Открытых критических и высоких нет")]
    return parts


def _detail(category: str, key: str, raw: dict) -> str:
    if category == MetricSample.Category.DOCS:
        return _docs_detail(key, raw)
    if category == MetricSample.Category.ACTIVITY:
        return _activity_detail(key, raw)
    if category == MetricSample.Category.ISSUES:
        return _issues_detail(key, raw)
    if category == MetricSample.Category.CI_CD:
        return _cicd_detail(key, raw)
    if category == MetricSample.Category.CODE_HEALTH:
        return _code_health_detail(key, raw)
    if category == MetricSample.Category.SECURITY:
        return _security_detail(key, raw)
    return ""


def _docs_detail(key: str, raw: dict) -> str:
    if key == "readme_quality":
        if raw.get("readme_present") is False:
            return "нет файла"
        size = _fmt_number(raw.get("readme_size_chars"))
        if size:
            return f"{size} символов"
    if key == "license":
        if raw.get("license_present") is False:
            return "нет файла"
        if raw.get("license_type_recognized") is True:
            return "тип распознан"
        if raw.get("license_type_recognized") is False:
            return "тип не распознан"
    return ""


def _activity_detail(key: str, raw: dict) -> str:
    if key == "recent_activity":
        days = _fmt_number(raw.get("last_activity_age_days"))
        if days:
            return f"{days} дн. назад"
    if key == "commits":
        count = _fmt_number(raw.get("commits_30d"))
        if count:
            return f"{count} за 30 дней"
    if key == "contributors":
        return _fmt_number(raw.get("contributors_count"))
    if key == "merge_requests":
        count = _fmt_number(raw.get("merge_requests_30d"))
        if count:
            return f"{count} за 30 дней"
    if key == "releases":
        count = _fmt_number(raw.get("releases_30d"))
        if count:
            return f"{count} за 30 дней"
    return ""


def _issues_detail(key: str, raw: dict) -> str:
    if key == "stale_ratio":
        return _fmt_ratio(raw.get("stale_ratio"))
    if key == "first_response":
        hours = _fmt_number(raw.get("median_first_response_hours"))
        if hours:
            return f"{hours} ч"
    if key == "time_to_close":
        days = _fmt_number(raw.get("median_time_to_close_days"))
        if days:
            return f"{days} дн."
    if key == "close_rate_30d":
        return _fmt_ratio(raw.get("close_rate_30d"))
    return ""


def _cicd_detail(key: str, raw: dict) -> str:
    if key == "ci_config_present":
        if raw.get("ci_config_present") is False:
            return "нет .sourcecraft/ci.yaml"
        if raw.get("success_rate") is None and raw.get("median_duration_minutes") is None:
            return "завершённых прогонов нет"
        return "файл есть"
    if key == "success_rate":
        return _fmt_ratio(raw.get("success_rate"))
    if key == "duration":
        minutes = _fmt_number(raw.get("median_duration_minutes"))
        if minutes:
            return f"{minutes} мин"
    return ""


def _code_health_detail(key: str, raw: dict) -> str:
    if key == "tests_present":
        return _yes_no(raw.get("tests_present"), yes="есть", no="не найдены")
    if key == "lint_config_present":
        return _yes_no(raw.get("lint_config_present"), yes="конфиг есть", no="конфига нет")
    if key == "no_committed_junk":
        kinds = []
        if raw.get("vendored_deps_present"):
            kinds.append("зависимости в git")
        if raw.get("generated_artifacts_present"):
            kinds.append("каталоги сборки")
        if raw.get("binary_junk_present"):
            kinds.append("бинарные файлы")
        if kinds:
            return ", ".join(kinds)
        flags = (
            raw.get("vendored_deps_present"),
            raw.get("generated_artifacts_present"),
            raw.get("binary_junk_present"),
        )
        if any(flag is False for flag in flags):
            return "лишних файлов нет"
        return ""
    if key == "structure":
        if raw.get("is_data_only_repo"):
            return "только данные, без кода"
        if raw.get("is_flat_dump"):
            return "файлы свалены в один каталог"
        if raw.get("is_data_only_repo") is False or raw.get("is_flat_dump") is False:
            return "код разложен по каталогам"
        return ""
    if key == "dependency_hygiene":
        manifest = raw.get("dependency_manifest_present")
        lockfile = raw.get("lockfile_present")
        if manifest and lockfile:
            return "манифест и lockfile"
        if manifest and lockfile is False:
            return "манифест есть, lockfile нет"
        if manifest is False:
            return "манифеста нет"
        return ""
    if key == "todo_debt":
        total = _fmt_number(raw.get("todo_total_count"))
        if not total:
            return ""
        if total == "0":
            return "нет"
        old = _fmt_number(raw.get("todo_old_count"))
        if raw.get("todo_age_available") and old and old != "0":
            return f"{total}, из них {old} старше полугода"
        return total
    return ""


def _security_detail(key: str, raw: dict) -> str:
    critical = _as_number(raw.get("open_critical_count"))
    high = _as_number(raw.get("open_high_count"))
    if critical is None and high is None:
        return ""
    critical_count = int(critical or 0)
    high_count = int(high or 0)
    transitive = int(_as_number(raw.get("transitive_critical_count")) or 0)
    transitive = min(max(transitive, 0), critical_count)
    direct = critical_count - transitive
    if key == "direct":
        if direct:
            return f"{direct} × −{SECURITY_DIRECT_CRITICAL_PENALTY}"
        return "нет"
    if key == "transitive":
        if transitive:
            return f"{transitive} × −{SECURITY_TRANSITIVE_CRITICAL_PENALTY}"
        return "нет"
    if key == "high":
        if high_count:
            return f"{high_count} × −{SECURITY_HIGH_PENALTY}"
        return "нет"
    return ""


def _yes_no(value, *, yes: str, no: str) -> str:
    if value is True:
        return yes
    if value is False:
        return no
    return ""


def _reason(raw: dict) -> str:
    text = str(raw.get("reason") or raw.get("error") or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if "404" in text and "scan" in lowered:
        return "нет скана AppSec"
    return plain_security_reason(text)


def _part(label: str, score: int | None = None, share: int | None = None, detail: str = "") -> dict:
    return {
        "label": label,
        "score": score,
        "share": share,
        "detail": detail,
    }


def _round_score(value) -> int | None:
    number = _as_number(value)
    if number is None:
        return None
    return int(round(number))


def _as_number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_number(value) -> str:
    number = _as_number(value)
    if number is None:
        return ""
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}".replace(".", ",")


def _fmt_ratio(value) -> str:
    number = _as_number(value)
    if number is None:
        return ""
    return f"{round(number * 100)}%"
