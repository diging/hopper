import csv
import datetime
import io
import re

# A partial ISO date: a 4-digit year, optionally a month, optionally a day.
# Day can only appear when month does, so "year-day" is impossible to express.
_PARTIAL_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?$")


def parse_partial_date(value):
    """Parse a partial ISO date string into a ``(date, precision)`` pair.

    Accepts ``YYYY``, ``YYYY-MM`` or ``YYYY-MM-DD``. A missing month/day
    defaults to 1 so the value still fits a ``DateField``; ``precision``
    ("year", "month" or "day") records how much was actually supplied so the
    padding can be ignored later. Blank or unparseable input returns
    ``(None, "")``.
    """
    match = _PARTIAL_DATE_RE.match((value or "").strip())
    if not match:
        return None, ""
    year, month, day = match.groups()
    try:
        if day is not None:
            return datetime.date(int(year), int(month), int(day)), "day"
        if month is not None:
            return datetime.date(int(year), int(month), 1), "month"
        return datetime.date(int(year), 1, 1), "year"
    except ValueError:
        return None, ""


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
