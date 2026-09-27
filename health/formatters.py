def format_rating(value) -> str:
    """3 208 или 119,5 — без хвостового ,0."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number <= 0:
        return "—"
    if number == int(number):
        return f"{int(number):,}".replace(",", "\u00a0")
    whole, frac = f"{number:,.1f}".split(".")
    return f"{whole.replace(',', '\u00a0')},{frac}"


def format_percentile(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    if number < 1:
        shown = f"{number:.1f}".rstrip("0").rstrip(".")
    else:
        shown = str(int(round(number)))
    return f"топ {shown}%"
