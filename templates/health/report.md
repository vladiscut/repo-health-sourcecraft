# Repo Health: {{ repo.org_slug }}/{{ repo.repo_slug }}

{% if repo.url %}- Репозиторий: {{ repo.url }}
{% endif %}- Язык: {{ repo.language|default:"не указан" }}
- Дата анализа: {% if analyzed_at %}{{ analyzed_at|date:"d.m.Y H:i" }}{% else %}нет завершённого скана{% endif %}

## Итоговый Score

{% if overall is not None %}**{{ overall }}/100**{% else %}**Нет данных** — ни по одной категории нет рассчитанного балла.{% endif %}

## Категории

{% for row in categories %}- **{{ row.label }}:** {% if row.total is not None %}{{ row.total }}/100{% else %}Нет данных{% if row.reason %} ({{ row.reason }}){% endif %}{% endif %}
{% empty %}- Нет данных по категориям
{% endfor %}
{% if has_null %}
Категории без данных не обнуляют итог: их вес перераспределяется между категориями, где балл есть.
{% endif %}

## Рекомендации

{% for finding in findings %}- **[{{ finding.get_severity_display }}]** {{ finding.title }}{% if finding.category %} ({{ finding.get_category_display }}){% endif %}
{% if finding.detail %}  - {{ finding.detail }}
{% endif %}{% if finding.recommendation %}  - Действие: {{ finding.recommendation }}
{% endif %}{% for ref in finding.evidence_refs %}{% if ref|slice:":4" == "http" %}  - {{ ref }}
{% endif %}{% endfor %}{% if finding.estimated_score_impact %}  - Ожидаемый прирост Repo Health Score: +{{ finding.estimated_score_impact }}
{% endif %}
{% empty %}- Рекомендаций нет — либо скан ещё не считал Score, либо проблем не найдено.
{% endfor %}
