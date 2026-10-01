import io

from django.test import TestCase

from ask.admin import _apply_zip_csv_metadata
from ask.admin_csv import import_names_csv, validate_partial_date
from ask.models import DocumentType, PDFResource


class ValidatePartialDateTests(TestCase):
    def test_full_date(self):
        self.assertEqual(validate_partial_date("2024-03-15"), "2024-03-15")

    def test_year_month(self):
        self.assertEqual(validate_partial_date("2024-03"), "2024-03")

    def test_year_only(self):
        self.assertEqual(validate_partial_date("2024"), "2024")

    def test_blank_or_none_returns_empty(self):
        self.assertEqual(validate_partial_date(""), "")
        self.assertEqual(validate_partial_date("   "), "")
        self.assertEqual(validate_partial_date(None), "")

    def test_impossible_calendar_dates_rejected(self):
        with self.assertRaises(ValueError):
            validate_partial_date("2024-13")
        with self.assertRaises(ValueError):
            validate_partial_date("2024-02-30")

    def test_non_iso_input_rejected(self):
        with self.assertRaises(ValueError):
            validate_partial_date("March 2024")
        with self.assertRaises(ValueError):
            validate_partial_date("24-03-15")


class ImportNamesCsvTests(TestCase):
    def test_counts_created_and_skipped_rows(self):
        DocumentType.objects.create(name="Existing")
        csv_file = io.BytesIO("﻿name\nReport\n\n  Brief  \nExisting\nReport\n".encode())
        created, skipped = import_names_csv(DocumentType, csv_file)
        self.assertEqual((created, skipped), (2, 4))
        self.assertEqual(
            list(DocumentType.objects.values_list("name", flat=True)),
            ["Brief", "Existing", "Report"],
        )


class ApplyZipCsvMetadataTests(TestCase):
    def test_creates_lookups_and_sets_fields(self):
        obj = PDFResource(title="Doc")
        warnings = _apply_zip_csv_metadata(obj, {
            "Date Published": "2023-06",
            "document_type": "Report",
            "document_author_institution": "WHO",
            "institution_type": "NGO",
        })
        self.assertEqual(warnings, [])
        self.assertEqual(obj.date_published, "2023-06")
        self.assertEqual(obj.document_type.name, "Report")
        self.assertEqual(obj.document_author_institution.name, "WHO")
        self.assertEqual(obj.institution_type.name, "NGO")
        self.assertTrue(DocumentType.objects.filter(name="Report").exists())

    def test_reuses_existing_lookup_row(self):
        existing = DocumentType.objects.create(name="Report")
        obj = PDFResource(title="Doc")
        _apply_zip_csv_metadata(obj, {"document_type": "Report"})
        self.assertEqual(obj.document_type.pk, existing.pk)
        self.assertEqual(DocumentType.objects.filter(name="Report").count(), 1)

    def test_blank_and_missing_columns_are_skipped(self):
        obj = PDFResource(title="Doc")
        warnings = _apply_zip_csv_metadata(obj, {"document_type": "  ", "Date Published": ""})
        self.assertEqual(warnings, [])
        self.assertEqual(obj.date_published, "")
        self.assertIsNone(obj.document_type_id)
        self.assertEqual(_apply_zip_csv_metadata(PDFResource(title="Doc"), {}), [])

    def test_invalid_date_warns_and_leaves_field_blank(self):
        obj = PDFResource(title="Doc")
        warnings = _apply_zip_csv_metadata(obj, {"Date Published": "not-a-date"})
        self.assertEqual(len(warnings), 1)
        self.assertIn("Date Published", warnings[0])
        self.assertEqual(obj.date_published, "")

    def test_title_case_headers_are_applied(self):
        obj = PDFResource(title="Doc")
        warnings = _apply_zip_csv_metadata(obj, {
            "Document Type": "Report",
            "Document Author Institution": "WHO",
            "Institution Type": "NGO",
            "Publisher": " State ",
        })
        self.assertEqual(warnings, [])
        self.assertEqual(obj.document_type.name, "Report")
        self.assertEqual(obj.document_author_institution.name, "WHO")
        self.assertEqual(obj.institution_type.name, "NGO")
        self.assertEqual(obj.publisher, "State")

    def test_overlong_lookup_value_warns_and_is_skipped(self):
        obj = PDFResource(title="Doc")
        warnings = _apply_zip_csv_metadata(obj, {"document_type": "x" * 256})
        self.assertEqual(warnings, ["document_type value exceeds 255 characters; left blank"])
        self.assertIsNone(obj.document_type_id)
        self.assertFalse(DocumentType.objects.exists())
