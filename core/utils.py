from datetime import datetime

from django.utils.dateparse import parse_datetime as parse_dt


def parse_datetime(value: str | None) -> datetime | None:
    """Разбирает ISO-8601 строку в datetime"""

    if not value:
        return None
    return parse_dt(value)


def to_int(value: object) -> int:
    """Приводит значение к int"""

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def to_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
