import csv
import io


def import_names_csv(model, file_obj):
    """Import a one-column CSV into a model with a ``name`` field.

    Returns ``(created, skipped)``. Blank rows, a leading header row of ``name``,
    and rows whose name already exists in the table are all counted as skipped.
    """
    text = file_obj.read().decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))

    created = 0
    skipped = 0
    for row in reader:
        name = row[0].strip() if row else ""
        if not name or name.lower() == "name":
            skipped += 1
            continue
        _, was_created = model.objects.get_or_create(name=name)
        if was_created:
            created += 1
        else:
            skipped += 1
    return created, skipped
