from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("test_platform", "0006_remove_ui_procedure_database"),
    ]

    operations = [
        migrations.RenameField(
            model_name="testresourceprofile",
            old_name="database_query_file",
            new_name="database_asset_file",
        ),
    ]
