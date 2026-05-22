from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ask", "0012_pdfresource_status_pdfresource_status_message_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="pdfresource",
            name="original_filename",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
