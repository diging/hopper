import csv
import datetime
import io


def normalize_partial_date(value):
    """Validate a partial ISO 8601 date string and return its trimmed form.

    Accepts ``YYYY``, ``YYYY-MM`` or ``YYYY-MM-DD`` (zero-padded). Calendar
    correctness is delegated to ``datetime.date.fromisoformat`` by padding
    the missing components with ``-01``. Empty / whitespace input returns
    ``""``; any other malformed value raises ``ValueError``.
    """
    s = (value or "").strip()
    if not s:
        return ""
    if len(s) == 4:
        datetime.date.fromisoformat(s + "-01-01")
    elif len(s) == 7:
        datetime.date.fromisoformat(s + "-01")
    elif len(s) == 10:
        datetime.date.fromisoformat(s)
    else:
        raise ValueError(f"not a partial ISO date: {value!r}")
    return s


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
