"""Прозрачный rule-based расчёт Repo Health Score.

Модуль намеренно не обращается к моделям Django, не делает запросов к БД
и не ходит в API SourceCraft — сюда вынесена вся "чистая" арифметика
скоринга

Модули вида ``health/issues_scan.py``, ``health/docs_scan.py`` и т.п.
отвечают только за сбор сырых метрик через SourceCraftClient и запись
результата в БД (MetricSample/HealthScore/Finding); сам расчёт балла
0-100 и data_completeness должен делаться функциями отсюда.
"""

from health.models import Finding, MetricSample


# Веса категорий. Сумма равна 1.0.
# файлы health/*_scan.py импортируют веса отсюда
CATEGORY_WEIGHTS = {
    MetricSample.Category.SECURITY: 0.20,
    MetricSample.Category.CODE_HEALTH: 0.20,
    MetricSample.Category.ACTIVITY: 0.15,
    MetricSample.Category.DOCS: 0.15,
    MetricSample.Category.CI_CD: 0.15,
    MetricSample.Category.ISSUES: 0.15,
}

CATEGORY_LABELS = {
    MetricSample.Category.DOCS: "Документация",
    MetricSample.Category.CI_CD: "CI/CD",
    MetricSample.Category.SECURITY: "Security",
    MetricSample.Category.ACTIVITY: "Активность",
    MetricSample.Category.ISSUES: "Issues",
    MetricSample.Category.CODE_HEALTH: "Code health",
}


# --------------------------------------------------------------------
# Переиспользуемые примитивы скоринга. Любая категория, которая сводит
# несколько сырых метрик к баллу 0-100, должна использовать именно их
# --------------------------------------------------------------------

def scale(value: float, worst: float, best: float) -> float:
    """Линейно переводит value из диапазона [worst..best] в [0..100].

    `worst`/`best` могут идти в любом порядке: если worst > best,
    это означает, что чем меньше value, тем лучше (например, "часы до
    первого ответа" — 168ч это плохо/worst, 24ч это хорошо/best).
    Результат всегда зажат в [0, 100].
    """

    if worst == best:
        return 100.0
    ratio = (value - worst) / (best - worst)
    ratio = max(0.0, min(1.0, ratio))
    return ratio * 100.0


def weighted_submetric_score(
    submetric_scores: dict[str, float],
    submetric_weights: dict[str, float],
) -> tuple[int | None, float]:
    """Схлопывает набор под-баллов 0-100 в единый балл категории.

    `submetric_scores` должен содержать только те под-метрики, которые
    реально удалось посчитать (недоступные исключает вызывающий код).
    Возвращает `(score_0_100_or_None, data_completeness_0_1)` —
    data_completeness это доля суммарного веса под-метрик, по которым
    вообще есть данные. Если данных нет вовсе — `(None, 0.0)`.
    """

    available_weight = sum(
        submetric_weights[key]
        for key in submetric_scores
        if key in submetric_weights
    )
    total_weight = sum(submetric_weights.values())
    if available_weight == 0.0 or total_weight == 0.0:
        return None, 0.0

    weighted_sum = sum(
        submetric_scores[key] * submetric_weights[key]
        for key in submetric_scores
        if key in submetric_weights
    )
    score = weighted_sum / available_weight
    data_completeness = available_weight / total_weight
    return round(score), round(data_completeness, 2)


_SEVERITY_ORDER = [
    Finding.Severity.LOW,
    Finding.Severity.MEDIUM,
    Finding.Severity.HIGH,
    Finding.Severity.CRITICAL,
]


def bump_severity(severity: str, category_score: float | None, threshold: float) -> str:
    """Поднимает severity находки на одну ступень, если категория просела.

    Та же по сути проблема заслуживает более высокого приоритета, если
    категория в целом и так тянет Repo Health Score вниз (`category_score
    < threshold`), чем если это единственная шероховатость на фоне
    здоровой категории. `category_score is None` (нет данных) не
    поднимает severity — поднимать не от чего.
    """

    if category_score is None or category_score >= threshold:
        return severity
    idx = _SEVERITY_ORDER.index(severity)
    return _SEVERITY_ORDER[min(idx + 1, len(_SEVERITY_ORDER) - 1)]


# --------------------------------------------------------------------
# Issues: пороги, веса под-метрик и сам расчёт балла категории.
# --------------------------------------------------------------------

ISSUES_STALE_DAYS_THRESHOLD = 30
ISSUES_LOOKBACK_DAYS = 30
ISSUES_FIRST_RESPONSE_SAMPLE_SIZE = 30

# Порог доли "зависших" открытых issues (не обновлялись > 30 дней),
# при котором под-балл stale_ratio падает до 0: 0% зависших = 100
# баллов, 50%+ зависших = 0 баллов, между ними — линейно.
ISSUES_STALE_RATIO_FOR_ZERO_SCORE = 0.5

# 168ч = 7 суток: медианное время до первого ответа >= недели — 0
# баллов (worst). <= 24ч — 100 баллов (best). Между ними — линейно.
ISSUES_FIRST_RESPONSE_HOURS_FOR_ZERO_SCORE = 168.0
ISSUES_FIRST_RESPONSE_HOURS_FOR_FULL_SCORE = 24.0

# Медианное время закрытия >= 90 дней — 0 баллов (worst). <= 7 дней —
# 100 баллов (best). Между ними — линейно.
ISSUES_TIME_TO_CLOSE_DAYS_FOR_ZERO_SCORE = 90.0
ISSUES_TIME_TO_CLOSE_DAYS_FOR_FULL_SCORE = 7.0

# close_rate_30d: 0% закрытых от созданных за период — 0 баллов
# (worst), 100%+ (уже обрезано min(1.0, ...) в issues_scan.py) — 100
# баллов (best).
ISSUES_LOW_CLOSE_RATE_FOR_ZERO_SCORE = 0.0
ISSUES_FULL_CLOSE_RATE_FOR_FULL_SCORE = 1.0

# Если итоговый балл категории Issues ниже этого порога — severity всех
# находок категории поднимается на ступень.
ISSUES_CATEGORY_SCORE_SEVERITY_BUMP_THRESHOLD = 40

# Сумма весов = 1.0, независимо от CATEGORY_WEIGHTS[ISSUES] (0.15),
# который применяется уже при смешивании всех 6 категорий между собой.
# stale_ratio — самый тяжёлый вес (прямой сигнал "забросили трекер"),
# close_rate_30d — самый лёгкий (шумная метрика на малых числах).
ISSUES_SUBMETRIC_WEIGHTS = {
    "stale_ratio": 0.35,
    "first_response": 0.30,
    "time_to_close": 0.20,
    "close_rate_30d": 0.15,
}


def score_issues_category(metrics) -> tuple[int | None, float, dict[str, float]]:
    """Считает балл категории Issues по уже собранным сырым метрикам.

    ``metrics`` — любой объект с атрибутами ``total_count``,
    ``open_count``, ``stale_ratio``, ``stale_count``,
    ``median_first_response_hours``, ``first_response_available``,
    ``median_time_to_close_days``, ``close_rate_30d`` (см.
    ``health.issues_scan._IssuesMetrics``). Чистая функция без
    обращений к БД/API.

    Возвращает ``(score_0_100_or_None, data_completeness_0_1,
    submetric_scores)``.
    """
    submetric_scores: dict[str, float] = {}

    if metrics.stale_ratio is not None:
        submetric_scores["stale_ratio"] = scale(
            metrics.stale_ratio,
            worst=ISSUES_STALE_RATIO_FOR_ZERO_SCORE,
            best=0.0,
        )
    else:
        # Нет открытых issues вообще — не штрафуем.
        submetric_scores["stale_ratio"] = 100.0

    if metrics.first_response_available and metrics.median_first_response_hours is not None:
        submetric_scores["first_response"] = scale(
            metrics.median_first_response_hours,
            worst=ISSUES_FIRST_RESPONSE_HOURS_FOR_ZERO_SCORE,
            best=ISSUES_FIRST_RESPONSE_HOURS_FOR_FULL_SCORE,
        )

    if metrics.median_time_to_close_days is not None:
        submetric_scores["time_to_close"] = scale(
            metrics.median_time_to_close_days,
            worst=ISSUES_TIME_TO_CLOSE_DAYS_FOR_ZERO_SCORE,
            best=ISSUES_TIME_TO_CLOSE_DAYS_FOR_FULL_SCORE,
        )

    if metrics.close_rate_30d is not None:
        submetric_scores["close_rate_30d"] = scale(
            metrics.close_rate_30d,
            worst=ISSUES_LOW_CLOSE_RATE_FOR_ZERO_SCORE,
            best=ISSUES_FULL_CLOSE_RATE_FOR_FULL_SCORE,
        )

    if metrics.total_count == 0:
        # У репозитория вообще нет issues — недостаточно данных для
        # оценки категории, а не "плохая" оценка.
        return None, 0.0, submetric_scores

    score, data_completeness = weighted_submetric_score(
        submetric_scores, ISSUES_SUBMETRIC_WEIGHTS
    )
    return score, data_completeness, submetric_scores


# --------------------------------------------------------------------
# Docs: пороги, веса под-метрик и сам расчёт балла категории.
# --------------------------------------------------------------------

# Если итоговый балл категории Docs ниже этого порога — severity всех
# находок категории поднимается на ступень.
DOCS_CATEGORY_SCORE_SEVERITY_BUMP_THRESHOLD = 40

# Сумма весов = 1.0, независимо от CATEGORY_WEIGHTS[DOCS] (0.15),
# который применяется уже при смешивании всех 6 категорий между собой.
# readme_quality — самый тяжёлый вес (входная точка проекта).
DOCS_SUBMETRIC_WEIGHTS = {
    "readme_quality": 0.30,
    "license": 0.20,
    "local_run": 0.15,
    "build_test": 0.15,
    "contributing_codeowners": 0.10,
    "structure_extras": 0.10,
}

# README: <500 символов — 0 баллов (worst), >=3000 символов — 100 (best).
DOCS_README_CHARS_FOR_ZERO_SCORE = 500
DOCS_README_CHARS_FOR_FULL_SCORE = 3000


def score_docs_category(metrics) -> tuple[int | None, float, dict[str, float]]:
    """Считает балл категории Docs по уже собранным сырым метрикам"""

    submetric_scores: dict[str, float] = {}

    # readme_quality: наличие + размер + ключевые разделы.
    # Считается ТОЛЬКО если контент реально прочитан (readme_size_chars
    # не None) — иначе это "нет данных", а не плохой/хороший балл, и
    # submetric не должен фабриковаться из дефолтов.
    if metrics.readme_present is False:
        submetric_scores["readme_quality"] = 0.0
    elif metrics.readme_present is True and metrics.readme_size_chars is not None:
        size_score = scale(
            metrics.readme_size_chars,
            worst=DOCS_README_CHARS_FOR_ZERO_SCORE,
            best=DOCS_README_CHARS_FOR_FULL_SCORE,
        )

        section_flags = [
            metrics.readme_has_local_run,
            metrics.readme_has_build_test,
            metrics.readme_has_structure,
        ]
        known = [bool(f) for f in section_flags if f is not None]
        sections_score = (
            sum(100.0 for f in known if f) / len(known) if known else 0.0
        )

        submetric_scores["readme_quality"] = (
            size_score * 0.5 + sections_score * 0.5
        )
    # else: readme_present is True, но контент не прочитан — недостаточно
    # данных для readme_quality, ключ намеренно не добавляется в
    # submetric_scores (не путать с readme_present is False = 0.0).

    # license: наличие + распознанный тип.
    if metrics.license_present is False:
        submetric_scores["license"] = 0.0
    elif metrics.license_present is True:
        if metrics.license_type_recognized is False:
            submetric_scores["license"] = 50.0
        elif metrics.license_type_recognized is True:
            submetric_scores["license"] = 100.0
        # license_type_recognized is None (файл не прочитан) — ключ не
        # добавляется, аналогично readme_quality выше.

    # ... остальное без изменений (local_run, build_test,
    # contributing_codeowners, structure_extras) — они и раньше корректно
    # исключали None через "if metrics.X is not None".

    if not submetric_scores:
        return None, 0.0, submetric_scores

    score, data_completeness = weighted_submetric_score(
        submetric_scores, DOCS_SUBMETRIC_WEIGHTS
    )
    return score, data_completeness, submetric_scores


# --------------------------------------------------------------------
# Ниже — исходные упрощённые оценщики по остальным категориям. Они
# заметно грубее, чем issues_scan.py/score_issues_category выше
# (бинарные пороги вместо плавной шкалы, фиктивные data_completeness).
# для docs/ci_cd/security/activity/code_health
# рекомендуется по образцу Issues написать отдельные *_scan.py модули,
# использующие scale()/weighted_submetric_score() выше, а не эти
# функции.
# --------------------------------------------------------------------

def _docs_score(raw: dict) -> int:
    return 80 if raw.get("has_readme") else 20


def _ci_cd_score(raw: dict) -> int:
    return 85 if raw.get("has_ci") and raw.get("releases") else 40


def _security_score(raw: dict) -> int:
    return 90 if raw.get("open_alerts", 0) == 0 else 35


def _activity_score(raw: dict) -> int:
    return 90 if raw.get("days_since_commit", 99) < 14 else 45


def _issues_score(raw: dict) -> int:
    open_issues = raw.get("open_issues")
    if open_issues is None:
        return 70
    if open_issues == 0:
        return 95
    if open_issues <= 10:
        return 75
    return 50


def _code_health_score(raw: dict) -> int:
    todo_count = raw.get("todo_count")
    if todo_count is None:
        return 65
    if todo_count <= 5:
        return 85
    if todo_count <= 20:
        return 65
    return 45


_CATEGORY_EVALUATORS = {
    MetricSample.Category.DOCS: _docs_score,
    MetricSample.Category.CI_CD: _ci_cd_score,
    MetricSample.Category.SECURITY: _security_score,
    MetricSample.Category.ACTIVITY: _activity_score,
    MetricSample.Category.ISSUES: _issues_score,
    MetricSample.Category.CODE_HEALTH: _code_health_score,
}


def _build_findings(raw: dict) -> list[dict]:
    """Создаёт список находок с severity, допустимой моделью Finding."""
    findings: list[dict] = []

    if not raw.get("has_ci"):
        findings.append(
            {
                "category": MetricSample.Category.CI_CD,
                "severity": Finding.Severity.HIGH,
                "title": "Нет CI-конфига",
                "detail": "В репозитории не найден пайплайн SourceCraft CI.",
                "recommendation": (
                    "Добавьте .sourcecraft/ci.yaml "
                    "с базовой проверкой сборки."
                ),
                "evidence_refs": [".sourcecraft/ci.yaml"],
                "estimated_score_impact": 10,
            }
        )

    if raw.get("open_alerts", 0):
        findings.append(
            {
                "category": MetricSample.Category.SECURITY,
                "severity": Finding.Severity.CRITICAL,
                "title": "Открытые AppSec-алерты",
                "detail": f"Найдено алертов: {raw['open_alerts']}.",
                "recommendation": (
                    "Закройте уязвимости зависимостей "
                    "или зафиксируйте ложные срабатывания."
                ),
                "evidence_refs": ["security/alerts"],
                "estimated_score_impact": 20,
            }
        )

    if raw.get("days_since_commit", 0) > 30:
        findings.append(
            {
                "category": MetricSample.Category.ACTIVITY,
                "severity": Finding.Severity.MEDIUM,
                "title": "Давно не было коммитов",
                "detail": (
                    f"Последняя активность "
                    f"{raw['days_since_commit']} дн. назад."
                ),
                "recommendation": (
                    "Проверьте, не заброшен ли проект, "
                    "и обновите статус в README."
                ),
                "evidence_refs": ["commits/HEAD"],
                "estimated_score_impact": 15,
            }
        )

    if not raw.get("releases"):
        findings.append(
            {
                "category": MetricSample.Category.CI_CD,
                "severity": Finding.Severity.LOW,
                "title": "Нет релизов",
                "detail": "Теги и релизы не найдены.",
                "recommendation": (
                    "Опубликуйте хотя бы один релиз, "
                    "чтобы потребители видели стабильную версию."
                ),
                "evidence_refs": ["releases"],
                "estimated_score_impact": 5,
            }
        )

    if not raw.get("has_readme"):
        findings.append(
            {
                "category": MetricSample.Category.DOCS,
                "severity": Finding.Severity.MEDIUM,
                "title": "Отсутствует README",
                "detail": "В репозитории не найден файл README.",
                "recommendation": (
                    "Добавьте README.md с описанием проекта "
                    "и инструкцией по запуску."
                ),
                "evidence_refs": ["README.md"],
                "estimated_score_impact": 8,
            }
        )

    return findings


def score_from_raw(raw: dict) -> tuple[dict, list[dict]]:
    """Считает score 0-100 по категориям упрощёнными оценщиками выше.

    См. предупреждение в комментарии над блоком — для реального расчёта
    категорий (кроме Issues, см. score_issues_category) эта функция
    слишком груба относительно требований ТЗ (см. анализ в описании
    задачи) и годится скорее как заглушка/пример.
    """
    scores: dict[str, dict] = {}
    for category, weight in CATEGORY_WEIGHTS.items():
        evaluator = _CATEGORY_EVALUATORS[category]
        total = evaluator(raw)
        scores[category] = {
            "total": total,
            "weight_used": weight,
            "data_completeness": 1.0,
            "raw_metrics": dict(raw),
        }

    findings = _build_findings(raw)
    return scores, findings


def overall_from_category_totals(totals: dict[str, int | None]) -> int | None:
    """Взвешенная сумма доступных категорий. None, если считать нечего."""
    used = 0.0
    acc = 0.0
    for category, weight in CATEGORY_WEIGHTS.items():
        value = totals.get(category)
        if value is None:
            continue
        acc += value * weight
        used += weight
    if used == 0:
        return None
    return round(acc / used)


def score_level(total: int | None) -> str:
    if total is None:
        return ""
    if total >= 80:
        return "ok"
    if total >= 50:
        return "mid"
    return "low"


def present_scores(totals: dict[str, int | None]) -> dict:
    """Данные для шаблона: итог, уровень, подписи категорий."""
    total = overall_from_category_totals(totals)
    categories = [
        (CATEGORY_LABELS.get(key, key), value)
        for key, value in totals.items()
        if value is not None
    ]
    return {
        "total": total,
        "level": score_level(total),
        "categories": categories,
        "totals": totals,
    }
