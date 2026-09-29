from django.db import migrations, models


def move_checking_scans(apps, schema_editor):
    """CHECKING жил на Scan как заглушка до анализа. Переносим его на доступ."""

    Scan = apps.get_model("health", "Scan")
    Access = apps.get_model("health", "UserRepositoryAccess")
    for scan in Scan.objects.filter(status="checking").iterator():
        if scan.triggered_by_user_id:
            Access.objects.filter(
                user_id=scan.triggered_by_user_id,
                repository_id=scan.repository_id,
            ).update(
                status="checking",
                checking_since=scan.created_at,
                check_error="",
            )
        scan.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("health", "0005_alter_finding_category_alter_finding_severity_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="userrepositoryaccess",
            name="status",
            field=models.CharField(
                choices=[
                    ("granted", "доступ есть"),
                    ("checking", "проверяем доступ"),
                ],
                default="granted",
                max_length=16,
                verbose_name="статус",
            ),
        ),
        migrations.AddField(
            model_name="userrepositoryaccess",
            name="checking_since",
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name="проверка доступа с",
            ),
        ),
        migrations.AddField(
            model_name="userrepositoryaccess",
            name="check_error",
            field=models.TextField(
                blank=True,
                default="",
                verbose_name="ошибка проверки доступа",
            ),
        ),
        migrations.RunPython(move_checking_scans, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="userrepositoryaccess",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "checking")),
                fields=("repository",),
                name="one_access_check_per_repo",
            ),
        ),
        migrations.AlterField(
            model_name="scan",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "в очереди"),
                    ("running", "идёт"),
                    ("success", "готово"),
                    ("failed", "ошибка"),
                    (
                        "partial",
                        "частично (часть данных недоступна)",
                    ),
                ],
                default="pending",
                max_length=10,
                verbose_name="статус",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="scan",
            name="one_active_scan_per_repo",
        ),
        migrations.AddConstraint(
            model_name="scan",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status__in", ["pending", "running"])),
                fields=("repository",),
                name="one_active_scan_per_repo",
            ),
        ),
    ]
