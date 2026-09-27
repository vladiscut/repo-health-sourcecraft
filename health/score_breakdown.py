"""Состав балла категории для карточки репозитория.

Читает уже сохранённый снимок HealthScore и не пересчитывает Score.
"""

from health.cicd_scan import SUBMETRIC_WEIGHTS as CICD_SUBMETRIC_WEIGHTS
from health.models import MetricSample
from health.scoring import (
    ACTIVITY_SUBMETRIC_WEIGHTS_WITH_COMMITS,
    ACTIVITY_SUBMETRIC_WEIGHTS_WITHOUT_COMMITS,
    CATEGORY_LABELS,
    DOCS_SUBMETRIC_WEIGHTS,
    ISSUES_SUBMETRIC_WEIGHTS,
)
from health.security_scan import (
    DIRECT_CRITICAL_PENALTY,
    HIGH_PENALTY,
    TRANSITIVE_CRITICAL_PENALTY,
)


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
        parts.append(_part("Прямые critical", detail=f"{direct} × −{DIRECT_CRITICAL_PENALTY}"))
    if transitive:
        parts.append(
            _part(
                "Транзитивные critical",
                detail=f"{transitive} × −{TRANSITIVE_CRITICAL_PENALTY}",
            )
        )
    if high_count:
        parts.append(_part("High", detail=f"{high_count} × −{HIGH_PENALTY}"))
    if not parts:
        return [_part("Открытых critical и high нет")]
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
    if key == "success_rate":
        return _fmt_ratio(raw.get("success_rate"))
    if key == "duration":
        minutes = _fmt_number(raw.get("median_duration_minutes"))
        if minutes:
            return f"{minutes} мин"
    return ""


def _reason(raw: dict) -> str:
    text = str(raw.get("reason") or raw.get("error") or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if "404" in text and "scan" in lowered:
        return "нет скана AppSec"
    return text


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
