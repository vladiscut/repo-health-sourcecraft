from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("health", "0002_repository_issues"),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE health_userrepositoryaccess DROP COLUMN IF EXISTS relation;",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql=(
                "DELETE FROM django_migrations "
                "WHERE app = 'health' "
                "AND name = '0003_userrepositoryaccess_relation';"
            ),
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
