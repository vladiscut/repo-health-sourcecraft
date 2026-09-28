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

# CI/CD: веса под-метрик (сумма = 1.0). Живут здесь, а не в
# health/cicd_scan.py, чтобы SUBMETRIC_WEIGHTS_BY_CATEGORY мог собрать
# карту всех шести категорий без циклического импорта.
CICD_SUBMETRIC_WEIGHTS = {
    "ci_config_present": 0.30,
    "success_rate": 0.55,
    "duration": 0.15,
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


def find_impact(
    submetric_key: str,
    submetric_weights: dict[str, float],
    submetric_scores: dict[str, float],
) -> int:
    """Сколько баллов КАТЕГОРИИ вернёт устранение проблемы по субметрике.

    Контракт `estimated_score_impact` находки (см. design.md D1): целое
    число 0-100, равное числу баллов категории, которое вернёт доведение
    субметрики до 100.

    Правило:

    - если `submetric_key` отсутствует в `submetric_scores` (метрику не
      удалось получить) или в `submetric_weights` — 0: исправление
      недоступной метрики балл не меняет;
    - иначе ``round(weight * (100 - score))``.

    Пример: ``contributing_codeowners`` с весом 0.15 и баллом 0 -> 15.
    """

    if submetric_key not in submetric_scores:
        return 0
    if submetric_key not in submetric_weights:
        return 0
    weight = submetric_weights[submetric_key]
    score = submetric_scores[submetric_key]
    return round(weight * (100.0 - score))


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
    "local_run": 0.10,
    "build_test": 0.10,
    "contributing_codeowners": 0.15,
    "structure_extras": 0.15,
}

# README: <500 символов — 0 баллов (worst), >=3000 символов — 100 (best).
DOCS_README_CHARS_FOR_ZERO_SCORE = 500
DOCS_README_CHARS_FOR_FULL_SCORE = 3000


def score_docs_category(metrics) -> tuple[int | None, float, dict[str, float]]:
    """Считает балл категории Docs по уже собранным сырым метрикам"""

    submetric_scores: dict[str, float] = {}

    # readme_quality: наличие + размер + ключевой раздел структуры.
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

    # local_run: инструкция локального запуска в README (standalone).
    if metrics.readme_has_local_run is not None:
        submetric_scores["local_run"] = (
            100.0 if metrics.readme_has_local_run else 0.0
        )

    # build_test: инструкция сборки/тестов в README (standalone).
    if metrics.readme_has_build_test is not None:
        submetric_scores["build_test"] = (
            100.0 if metrics.readme_has_build_test else 0.0
        )

    # contributing_codeowners: вклад и владельцы кода.
    contrib_flags = [metrics.contributing_present, metrics.codeowners_present]
    known_contrib = [bool(f) for f in contrib_flags if f is not None]
    if known_contrib:
        submetric_scores["contributing_codeowners"] = (
            sum(100.0 for f in known_contrib if f) / len(known_contrib)
        )

    # structure_extras: дополнительные структурные сигналы документации.
    extras_flags = [
        metrics.changelog_present,
        metrics.docs_dir_present,
        metrics.issue_templates_present,
        metrics.pr_template_present,
    ]
    known_extras = [bool(f) for f in extras_flags if f is not None]
    if known_extras:
        submetric_scores["structure_extras"] = (
            sum(100.0 for f in known_extras if f) / len(known_extras)
        )

    if not submetric_scores:
        return None, 0.0, submetric_scores

    score, data_completeness = weighted_submetric_score(
        submetric_scores, DOCS_SUBMETRIC_WEIGHTS
    )
    return score, data_completeness, submetric_scores


# --------------------------------------------------------------------
# Activity: пороги, веса под-метрик и расчёт балла категории.
# --------------------------------------------------------------------

ACTIVITY_CATEGORY_SCORE_SEVERITY_BUMP_THRESHOLD = 40

# Два набора весов, сумма каждого = 1.0, независимо от
# CATEGORY_WEIGHTS[ACTIVITY] (0.15) — перенормировка между всеми 6
# категориями это отдельный шаг в health.orchestrator.aggregate_scan.
#
# WITHOUT_COMMITS — для массового планового скана (Scan.TriggeredBy.
# SCHEDULE): commits туда не входят вообще, будто их и не было в схеме —
# это тот же набор весов, что использовался до появления commits.
# Причина: подсчёт commits требует git clone, а массовый скан бьёт по
# тысячам публичных репозиториев — считать коммиты в этом сценарии
# ресурсно неприемлемо
#
# WITH_COMMITS — для одиночного ручного скана одного репозитория
# (Scan.TriggeredBy.USER / MANUAL): нагрузка минимальна (один git clone
# на запуск), поэтому commits учитываются.
ACTIVITY_SUBMETRIC_WEIGHTS_WITHOUT_COMMITS = {
    "recent_activity": 0.40,
    "merge_requests": 0.30,
    "releases": 0.30,
}

ACTIVITY_SUBMETRIC_WEIGHTS_WITH_COMMITS = {
    "recent_activity": 0.35,
    "commits": 0.20,
    "merge_requests": 0.25,
    "releases": 0.20,
}

# Для recent_activity:
# <= 7 дней — 100 баллов;
# 7..30 дней — 100 -> 60 линейно;
# 30..90 дней — 60 -> 0 линейно;
# > 90 дней — 0.
ACTIVITY_RECENT_ACTIVITY_FULL_DAYS = 7
ACTIVITY_RECENT_ACTIVITY_MID_DAYS = 30
ACTIVITY_RECENT_ACTIVITY_STALE_DAYS = 90

ACTIVITY_LOOKBACK_DAYS = 30

# Merge requests за ACTIVITY_LOOKBACK_DAYS: 0 -> 0; 10+ -> 100.
ACTIVITY_MERGE_REQUESTS_FOR_FULL_SCORE = 10

# Releases за ACTIVITY_LOOKBACK_DAYS: 0 -> 0; 3+ -> 100.
ACTIVITY_RELEASES_FOR_FULL_SCORE = 3

# Commits за ACTIVITY_LOOKBACK_DAYS: 0 -> 0; 1 -> 15; 20+ (~5/неделю) -> 100.
# Считается только при include_commits=True.
ACTIVITY_SINGLE_COMMIT_SCORE = 15
ACTIVITY_COMMITS_FOR_FULL_SCORE = 20


def _score_recent_activity(last_activity_age_days: float | None) -> float | None:
    if last_activity_age_days is None:
        return None
    age = max(0.0, last_activity_age_days)
    if age <= ACTIVITY_RECENT_ACTIVITY_FULL_DAYS:
        return 100.0
    if age <= ACTIVITY_RECENT_ACTIVITY_MID_DAYS:
        span = ACTIVITY_RECENT_ACTIVITY_MID_DAYS - ACTIVITY_RECENT_ACTIVITY_FULL_DAYS
        return 100.0 - ((age - ACTIVITY_RECENT_ACTIVITY_FULL_DAYS) / span * 40.0)
    if age <= ACTIVITY_RECENT_ACTIVITY_STALE_DAYS:
        span = ACTIVITY_RECENT_ACTIVITY_STALE_DAYS - ACTIVITY_RECENT_ACTIVITY_MID_DAYS
        return 60.0 - ((age - ACTIVITY_RECENT_ACTIVITY_MID_DAYS) / span * 60.0)
    return 0.0


def _score_activity_count(
    count: int | None,
    *,
    full_score_count: int,
    score_at_one: float = 0.0,
) -> float | None:
    if count is None:
        return None
    count = max(0, count)
    if count == 0:
        return 0.0
    if count == 1:
        return score_at_one
    if full_score_count <= 1:
        return 100.0
    ratio = (count - 1) / (full_score_count - 1)
    return min(100.0, score_at_one + ratio * (100.0 - score_at_one))


def score_activity_category(
    metrics,
    include_commits: bool,
) -> tuple[int | None, float, dict[str, float]]:
    """Считает балл категории Activity по уже собранным сырым метрикам.

    ``metrics`` — объект с атрибутами:

    - ``last_activity_age_days``
    - ``merge_requests_30d``
    - ``releases_30d``
    - ``commits_30d`` — используется, только если ``include_commits=True``

    ``include_commits`` решает, каким набором весов считать балл:
    - True  — ACTIVITY_SUBMETRIC_WEIGHTS_WITH_COMMITS (ручной скан одного
      репозитория, commits реально посчитаны через git clone);
    - False — ACTIVITY_SUBMETRIC_WEIGHTS_WITHOUT_COMMITS (массовый плановый
      скан, commits сознательно не считаются, вес размазан по остальным
      под-метрикам, как будто commits нет в схеме вовсе).

    Возвращает ``(score_0_100_or_None, data_completeness_0_1, submetric_scores)``.
    """

    weights = (
        ACTIVITY_SUBMETRIC_WEIGHTS_WITH_COMMITS
        if include_commits
        else ACTIVITY_SUBMETRIC_WEIGHTS_WITHOUT_COMMITS
    )
    submetric_scores: dict[str, float] = {}

    recent_activity_score = _score_recent_activity(metrics.last_activity_age_days)
    if recent_activity_score is not None:
        submetric_scores["recent_activity"] = recent_activity_score

    if include_commits:
        commits_score = _score_activity_count(
            metrics.commits_30d,
            full_score_count=ACTIVITY_COMMITS_FOR_FULL_SCORE,
            score_at_one=ACTIVITY_SINGLE_COMMIT_SCORE,
        )
        if commits_score is not None:
            submetric_scores["commits"] = commits_score

    merge_requests_score = _score_activity_count(
        metrics.merge_requests_30d,
        full_score_count=ACTIVITY_MERGE_REQUESTS_FOR_FULL_SCORE,
    )
    if merge_requests_score is not None:
        submetric_scores["merge_requests"] = merge_requests_score

    releases_score = _score_activity_count(
        metrics.releases_30d,
        full_score_count=ACTIVITY_RELEASES_FOR_FULL_SCORE,
    )
    if releases_score is not None:
        submetric_scores["releases"] = releases_score

    return (*weighted_submetric_score(submetric_scores, weights), submetric_scores)


# --------------------------------------------------------------------
# Code health: пороги, веса под-метрик и расчёт балла категории.
# --------------------------------------------------------------------

CODE_HEALTH_CATEGORY_SCORE_SEVERITY_BUMP_THRESHOLD = 40

# Сумма весов = 1.0, независимо от CATEGORY_WEIGHTS[CODE_HEALTH] (0.20).
#
# tests_present и no_committed_junk — самые тяжёлые: отсутствие тестов
# и замусоренное дерево (вендоренные зависимости/сборки в git) — самые
# прямые признаки проблем с поддерживаемостью. todo_debt — самый
# лёгкий: сам факт наличия TODO — это норма, штрафуем только за объём
# и застарелость.
CODE_HEALTH_SUBMETRIC_WEIGHTS = {
    "tests_present": 0.25,
    "no_committed_junk": 0.20,
    "structure": 0.15,
    "lint_config_present": 0.15,
    "dependency_hygiene": 0.10,
    "todo_debt": 0.15,
}

CODE_HEALTH_SUBMETRIC_WEIGHTS_WITHOUT_TODO_AGE = {
    "tests_present": 0.30,
    "no_committed_junk": 0.25,
    "structure": 0.17,
    "lint_config_present": 0.17,
    "dependency_hygiene": 0.11,
    "todo_debt": 0.05,
}


# --------------------------------------------------------------------
# Security: веса под-метрик и расчёт балла категории.
# --------------------------------------------------------------------

# Сумма весов = 1.0, независимо от CATEGORY_WEIGHTS[SECURITY] (0.20).
# Штрафы за открытые группы уязвимостей: direct critical тяжелее
# транзитивного, high — самый лёгкий. Балл категории = 100 минус
# сумма штрафов; submetric-баллы выражают «здоровье» по каждой
# критичности (100 при отсутствии открытых групп, 0 при упоре в
# максимальный штраф).
SECURITY_DIRECT_CRITICAL_PENALTY = 30
SECURITY_TRANSITIVE_CRITICAL_PENALTY = 20
SECURITY_HIGH_PENALTY = 10

SECURITY_SUBMETRIC_WEIGHTS = {
    "direct": 0.50,      # SECURITY_DIRECT_CRITICAL_PENALTY
    "transitive": 0.35,  # SECURITY_TRANSITIVE_CRITICAL_PENALTY
    "high": 0.15,        # SECURITY_HIGH_PENALTY
}


def score_security_category(metrics) -> tuple[int | None, float, dict[str, float]]:
    """Считает балл категории Security по уже собранным сырым метрикам.

    ``metrics`` — любой объект с атрибутами ``direct_penalty_sum``,
    ``transitive_penalty_sum`` и ``high_penalty_sum`` — суммарные штрафы
    (в баллах 0-100) по открытым группам соответствующей критичности
    (см. ``health.security_scan``). Чистая функция без обращений к
    БД/API, как и остальные ``score_*`` в этом модуле.

    submetric-баллы: 100 — открытых групп этой критичности нет (штраф 0),
    0 — штраф достиг максимума (равен весу категории * 100). Между
    ними — линейно. Возвращает ``(score_0_100_or_None,
    data_completeness_0_1, submetric_scores)``.
    """

    submetric_scores: dict[str, float] = {
        "direct": scale(
            metrics.direct_penalty_sum,
            worst=SECURITY_DIRECT_CRITICAL_PENALTY,
            best=0.0,
        ),
        "transitive": scale(
            metrics.transitive_penalty_sum,
            worst=SECURITY_TRANSITIVE_CRITICAL_PENALTY,
            best=0.0,
        ),
        "high": scale(
            metrics.high_penalty_sum,
            worst=SECURITY_HIGH_PENALTY,
            best=0.0,
        ),
    }

    score, data_completeness = weighted_submetric_score(
        submetric_scores, SECURITY_SUBMETRIC_WEIGHTS
    )
    return score, data_completeness, submetric_scores


# --------------------------------------------------------------------
# Единая карта "категория -> её веса субметрик". Единственное место, где
# перечислены все шесть категорий сразу: используется для валидации
# (сумма весов = 1.0) и как точка входа для find_impact().
#
# Activity здесь представлена ОБОИМИ наборами (with/without commits),
# потому что выбор набора зависит от типа скана, а не от категории.
# --------------------------------------------------------------------
SUBMETRIC_WEIGHTS_BY_CATEGORY = {
    MetricSample.Category.DOCS: DOCS_SUBMETRIC_WEIGHTS,
    MetricSample.Category.ISSUES: ISSUES_SUBMETRIC_WEIGHTS,
    MetricSample.Category.CODE_HEALTH: CODE_HEALTH_SUBMETRIC_WEIGHTS,
    MetricSample.Category.CI_CD: CICD_SUBMETRIC_WEIGHTS,
    MetricSample.Category.ACTIVITY: {
        "with_commits": ACTIVITY_SUBMETRIC_WEIGHTS_WITH_COMMITS,
        "without_commits": ACTIVITY_SUBMETRIC_WEIGHTS_WITHOUT_COMMITS,
    },
    MetricSample.Category.SECURITY: SECURITY_SUBMETRIC_WEIGHTS,
}

# todo_debt: количество TODO/FIXME, при котором балл падает до 0 (сам
# факт большого объёма долга), и порог "старых" (давность в днях),
# при превышении доли которых добавляется отдельный штраф.
CODE_HEALTH_TODO_COUNT_FOR_ZERO_SCORE = 40
CODE_HEALTH_TODO_OLD_AGE_DAYS = 180  # полгода — см. пример ТЗ ("старше шести месяцев")
CODE_HEALTH_TODO_OLD_RATIO_FOR_MAX_PENALTY = 0.5  # 50%+ старых -> полный доп. штраф
CODE_HEALTH_TODO_OLD_RATIO_PENALTY_POINTS = 30.0


def _score_no_committed_junk(metrics) -> float | None:
    """100, если в дереве нет закоммиченных зависимостей/сборок/бинарного
    мусора; каждая обнаруженная категория мусора вычитает штраф.

    None, если ни одного из трёх признаков не удалось определить (дерево
    не получено вообще) — тогда submetric попросту не участвует.
    """

    flags = [
        metrics.vendored_deps_present,
        metrics.generated_artifacts_present,
        metrics.binary_junk_present,
    ]
    known = [f for f in flags if f is not None]
    if not known:
        return None
    penalty_per_flag = 100.0 / len(known)
    return max(0.0, 100.0 - penalty_per_flag * sum(1 for f in known if f))


def _score_structure(metrics) -> float | None:
    """Оценивает "не свалка ли это" и "не data-only ли это".

    data-only репозиторий — это не код, поэтому по определению плохой
    сигнал для категории Code health
    """

    if metrics.is_data_only_repo is None and metrics.is_flat_dump is None:
        return None
    if metrics.is_data_only_repo:
        return 0.0
    if metrics.is_flat_dump:
        return 20.0
    return 100.0


def _score_dependency_hygiene(metrics) -> float | None:
    """Наличие манифеста зависимостей + lockfile.

    Если в репозитории вообще нет исходного кода (data-only / пусто) —
    submetric неприменим, а не "плохой": штрафовать за отсутствие
    package.json репозиторий из одних CSV бессмысленно, это уже
    отражено в submetric structure.
    """

    if not metrics.source_files_count:
        return None
    if metrics.dependency_manifest_present and metrics.lockfile_present:
        return 100.0
    if metrics.dependency_manifest_present:
        return 70.0
    return 30.0


def _score_todo_debt(metrics) -> float | None:
    if metrics.todo_total_count is None:
        return None
    if metrics.todo_total_count == 0:
        return 100.0

    count_score = scale(
        metrics.todo_total_count,
        worst=CODE_HEALTH_TODO_COUNT_FOR_ZERO_SCORE,
        best=0.0,
    )

    if metrics.todo_age_available and metrics.todo_old_count is not None:
        old_ratio = metrics.todo_old_count / metrics.todo_total_count
        penalty = scale(
            old_ratio,
            worst=CODE_HEALTH_TODO_OLD_RATIO_FOR_MAX_PENALTY,
            best=0.0,
        )
        # `scale` возвращает 0..100 "чем лучше, тем больше" — здесь
        # нужен ШТРАФ, поэтому берём (100 - penalty) как долю
        # применяемого максимального штрафа.
        penalty_points = (100.0 - penalty) / 100.0 * CODE_HEALTH_TODO_OLD_RATIO_PENALTY_POINTS
        count_score = max(0.0, count_score - penalty_points)

    return count_score


def score_code_health_category(
    metrics, include_todo_age: bool = True
) -> tuple[int | None, float, dict[str, float]]:
    """Считает балл категории Code health по уже собранным сырым метрикам.

    ``metrics`` — объект вида health.code_health_scan._CodeHealthMetrics.
    Чистая функция без обращений к БД/API, как и остальные score_*
    в этом модуле.

    ``include_todo_age`` выбирает набор весов под-метрик: когда давность
    TODO не считается (массовый плановый скан, публичный репозиторий),
    submetric ``todo_debt`` не получает штраф за застарелость, поэтому
    его вклад перераспределяется на остальные под-метрики через
    CODE_HEALTH_SUBMETRIC_WEIGHTS_WITHOUT_TODO_AGE. По умолчанию True —
    обратная совместимость с прежним поведением.
    """

    submetric_scores: dict[str, float] = {}

    if metrics.tests_present is not None:
        submetric_scores["tests_present"] = 100.0 if metrics.tests_present else 0.0

    if metrics.lint_config_present is not None:
        submetric_scores["lint_config_present"] = (
            100.0 if metrics.lint_config_present else 0.0
        )

    junk_score = _score_no_committed_junk(metrics)
    if junk_score is not None:
        submetric_scores["no_committed_junk"] = junk_score

    structure_score = _score_structure(metrics)
    if structure_score is not None:
        submetric_scores["structure"] = structure_score

    dependency_score = _score_dependency_hygiene(metrics)
    if dependency_score is not None:
        submetric_scores["dependency_hygiene"] = dependency_score

    todo_score = _score_todo_debt(metrics)
    if todo_score is not None:
        submetric_scores["todo_debt"] = todo_score

    if not submetric_scores:
        return None, 0.0, submetric_scores

    weights = (
        CODE_HEALTH_SUBMETRIC_WEIGHTS
        if include_todo_age
        else CODE_HEALTH_SUBMETRIC_WEIGHTS_WITHOUT_TODO_AGE
    )

    score, data_completeness = weighted_submetric_score(
        submetric_scores, weights
    )
    return score, data_completeness, submetric_scores


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


def _weighted_overall(
    totals: dict[str, int | None],
    weights: dict[str, float],
) -> int | None:
    """Взвешенная сумма категорий с явными весами.

    Аналог overall_from_category_totals(), но принимает произвольный
    набор весов (например, ренормализованный), а не CATEGORY_WEIGHTS.
    None, если считать нечего.
    """
    used = 0.0
    acc = 0.0
    for category, weight in weights.items():
        value = totals.get(category)
        if value is None:
            continue
        acc += value * weight
        used += weight
    if used == 0:
        return None
    return round(acc / used)


def compute_finding_impact(
    category: str,
    submetric_scores: dict[str, float],
    submetric_weights: dict[str, float],
    all_category_totals: dict[str, int | None],
    renormalized_category_weights: dict[str, float],
    fixed_submetrics: dict[str, float],
) -> int | None:
    """Честный дифференциальный estimated_score_impact для finding.

    Пересчитывает балл категории с под-метриками, заменёнными на
    "починенные" значения из fixed_submetrics, затем пересчитывает
    общий балл с теми же ренормализованными весами категорий и
    возвращает положительную дельту (0, если роста нет).

    Возвращает None, если исходный общий балл посчитать нельзя или
    для категории отсутствуют данные.
    """
    current_overall = _weighted_overall(
        all_category_totals, renormalized_category_weights
    )
    if current_overall is None:
        return None

    # Категория должна присутствовать с непустым баллом, иначе
    # дифференциал не определён.
    category_total = all_category_totals.get(category)
    if category_total is None:
        return None

    fixed_scores = dict(submetric_scores)
    fixed_scores.update(fixed_submetrics)
    new_category_total, _ = weighted_submetric_score(
        fixed_scores, submetric_weights
    )
    if new_category_total is None:
        return None

    new_totals = dict(all_category_totals)
    new_totals[category] = new_category_total
    new_overall = _weighted_overall(
        new_totals, renormalized_category_weights
    )
    if new_overall is None:
        return None

    delta = new_overall - current_overall
    if delta <= 0:
        return 0

    # Потолок: не больше оставшегося запаса до 100.
    headroom = 100 - current_overall
    if headroom < 0:
        headroom = 0
    return min(delta, headroom)


def score_level(total: int | None) -> str:
    if total is None:
        return ""
    if total >= 80:
        return "ok"
    if total >= 50:
        return "mid"
    return "low"


def present_scores(totals: dict[str, int | None]) -> dict:
    """Данные для шаблона: итог, уровень и все шесть категорий.

    Категория без балла остаётся в списке со значением None.
    Шаблон показывает её как «Нет данных», а не как 0.
    """
    total = overall_from_category_totals(totals)
    categories = [
        (label, totals.get(key))
        for key, label in CATEGORY_LABELS.items()
    ]
    return {
        "total": total,
        "level": score_level(total),
        "categories": categories,
        "totals": totals,
    }
