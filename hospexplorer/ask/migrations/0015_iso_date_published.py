"""Switch date_published from (DateField + precision enum) to a single
CharField holding a partial ISO 8601 date string.

The MissingMigration plus an AlterField wouldn't carry the precision
information across, so this migration: adds a temporary CharField, copies
each row's old (date, precision) pair into a partial ISO string, removes
the old fields, then renames the temp field to the canonical name.
"""
from django.db import migrations, models


def _to_iso_partial(date, precision):
    if date is None:
        return ""
    if precision == "year":
        return f"{date.year:04d}"
    if precision == "month":
        return f"{date.year:04d}-{date.month:02d}"
    # "day" — or any other / empty precision, which we treat as a full date
    return date.isoformat()


def forwards(apps, schema_editor):
    for model_name in ("PDFResource", "WebsiteResource"):
        Model = apps.get_model("ask", model_name)
        for obj in Model.objects.all():
            obj.date_published_iso = _to_iso_partial(
                obj.date_published, obj.date_published_precision
            )
            obj.save(update_fields=["date_published_iso"])


class Migration(migrations.Migration):

    dependencies = [
        ("ask", "0014_merge_20260526_2133"),
    ]

    operations = [
        migrations.AddField(
            model_name="pdfresource",
            name="date_published_iso",
            field=models.CharField(blank=True, default="", max_length=10),
        ),
        migrations.AddField(
            model_name="websiteresource",
            name="date_published_iso",
            field=models.CharField(blank=True, default="", max_length=10),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveField(model_name="pdfresource", name="date_published"),
        migrations.RemoveField(model_name="websiteresource", name="date_published"),
        migrations.RemoveField(model_name="pdfresource", name="date_published_precision"),
        migrations.RemoveField(model_name="websiteresource", name="date_published_precision"),
        migrations.RenameField(
            model_name="pdfresource",
            old_name="date_published_iso",
            new_name="date_published",
        ),
        migrations.RenameField(
            model_name="websiteresource",
            old_name="date_published_iso",
            new_name="date_published",
        ),
    ]
