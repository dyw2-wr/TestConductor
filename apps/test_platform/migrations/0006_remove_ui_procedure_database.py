from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("test_platform", "0005_alter_testplanartifact_resource_profile_and_more"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="testresourceprofile",
            name="ui_procedure_database",
        ),
    ]
