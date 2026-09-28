"""Порядок публичного списка по Score, который виден в таблице."""

from django.db.models import FloatField
from django.db.models.expressions import RawSQL

from health.models import HealthScore, Repository, Scan
from health.scoring import CATEGORY_WEIGHTS


def annotate_visible_score(queryset):
    """Добавляет visible_score: итог последнего завершённого скана.

    Формула та же, что у колонки Score: взвешенное среднее категорий,
    где балл есть. Если считать нечего, значение пустое — такие строки
    в сортировке по Score идут последними.

    Кэш ``Repository.health_score`` для этого не подходит: он может быть
    заполнен, когда в таблице уже «нет данных», или не совпадать с числом
    в колонке.
    """

    repo = Repository._meta.db_table
    score = HealthScore._meta.db_table
    scan = Scan._meta.db_table
    placeholders = ", ".join(["(%s, %s)"] * len(CATEGORY_WEIGHTS))
    params: list = []
    for category, weight in CATEGORY_WEIGHTS.items():
        params.extend([str(category), weight])
    sql = f"""
        (
          SELECT SUM(hs.total * w.weight) / NULLIF(SUM(w.weight), 0)
          FROM {score} hs
          JOIN (VALUES {placeholders}) AS w(category, weight)
            ON hs.category = w.category
          WHERE hs.total IS NOT NULL
            AND hs.scan_id = (
              SELECT s.id
              FROM {scan} s
              WHERE s.repository_id = {repo}.id
                AND s.status IN ('success', 'partial')
              ORDER BY s.created_at DESC
              LIMIT 1
            )
        )
    """
    return queryset.annotate(
        visible_score=RawSQL(sql, params, output_field=FloatField())
    )
