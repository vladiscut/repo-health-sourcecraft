from django.db import migrations


def drop_legacy_relation(apps, schema_editor):
    """Колонка relation осталась только в старых базах PostgreSQL.

    В текущей схеме её нет, а SQLite не понимает DROP COLUMN IF EXISTS.
    """
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        "ALTER TABLE health_userrepositoryaccess DROP COLUMN IF EXISTS relation;"
    )
    schema_editor.execute(
        "DELETE FROM django_migrations "
        "WHERE app = 'health' "
        "AND name = '0003_userrepositoryaccess_relation';"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("health", "0002_repository_issues"),
    ]

    operations = [
        migrations.RunPython(drop_legacy_relation, migrations.RunPython.noop),
    ]
