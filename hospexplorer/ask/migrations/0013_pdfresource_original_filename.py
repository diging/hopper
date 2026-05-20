import os
import re

from django.db import migrations, models


# Django's storage appends "_<7 random alphanumeric chars>" to a file name
# whenever the target name already exists. Strip that to recover the original.
_STORAGE_SUFFIX = re.compile(r"_[A-Za-z0-9]{7}$")


def backfill_original_filename(apps, schema_editor):
    PDFResource = apps.get_model("ask", "PDFResource")
    for resource in PDFResource.objects.all():
        if not resource.file or resource.original_filename:
            continue
        root, ext = os.path.splitext(os.path.basename(resource.file.name))
        resource.original_filename = f"{_STORAGE_SUFFIX.sub('', root)}{ext}"
        resource.save(update_fields=["original_filename"])


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
        migrations.RunPython(backfill_original_filename, migrations.RunPython.noop),
    ]
