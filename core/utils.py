from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.utils.dateparse import parse_datetime as parse_dt


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return parse_dt(value)


def to_int(value: object) -> int:
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


def get_scan_repo_dir(scna_id):
    path = f'{settings.SCAN_REPO_DIR}/{scna_id}'
    Path(path).mkdir(parents=True, exist_ok=True)
    return path
