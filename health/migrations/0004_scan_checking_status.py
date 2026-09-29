from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("health", "0003_drop_userrepositoryaccess_relation"),
    ]

    operations = [
        migrations.AlterField(
            model_name="scan",
            name="status",
            field=models.CharField(
                choices=[
                    ("checking", "проверяем доступ"),
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
                condition=models.Q(
                    ("status__in", ["checking", "pending", "running"])
                ),
                fields=("repository",),
                name="one_active_scan_per_repo",
            ),
        ),
    ]
