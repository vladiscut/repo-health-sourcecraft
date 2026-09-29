from health.models import Repository, Scan


def scan_for_card(repo: Repository) -> tuple[Scan | None, str]:
    """Один скан целиком: метрики и Score из него же.

    FAILED → предыдущий SUCCESS + явный текст.
    PARTIAL → этот PARTIAL со своим Score и пояснением.
    SUCCESS → как есть.
    """

    latest = (
        repo.scans.filter(
            status__in=[
                Scan.Status.SUCCESS,
                Scan.Status.PARTIAL,
                Scan.Status.FAILED,
            ]
        )
        .order_by("-created_at")
        .first()
    )
    if latest is None:
        return None, ""

    if latest.status == Scan.Status.FAILED:
        success = (
            repo.scans.filter(status=Scan.Status.SUCCESS)
            .order_by("-created_at")
            .first()
        )
        if success is None:
            notice = (
                "Последний анализ завершился с ошибкой, "
                "успешного скана ещё нет."
            )
            if latest.error:
                notice = f"{notice} {latest.error}"
            return None, notice
        when = (
            success.finished_at.strftime("%d.%m.%Y %H:%M")
            if success.finished_at
            else ""
        )
        notice = (
            "Последний анализ завершился с ошибкой. "
            "Показаны результаты предыдущего успешного скана"
            + (f" от {when}." if when else ".")
        )
        if latest.error:
            notice = f"{notice} {latest.error}"
        return success, notice

    if latest.status == Scan.Status.PARTIAL:
        notice = (
            "Анализ частичный: часть категорий не записалась. "
            "Итог — только по доступным данным."
        )
        if latest.error:
            notice = f"{notice} {latest.error}"
        return latest, notice

    return latest, ""
