from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("test_platform", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="testresourceprofile",
            name="ui_agent_asset_file",
            field=models.FileField(
                blank=True,
                help_text="上传包含 URL、功能和最大步数的表格或常见文本文件。",
                upload_to="test_platform/resources/ui-agent/%Y/%m/",
                verbose_name="网页 Agent 资料文件（可选）",
            ),
        ),
        migrations.AddField(
            model_name="testresourceprofile",
            name="ui_agent_asset_text",
            field=models.TextField(
                blank=True,
                help_text="直接填写网站 URL、大致功能和最大步数；与资料文件二选一。",
                verbose_name="网页 Agent 资料说明（可选）",
            ),
        ),
    ]
