import io
import shutil
import tempfile
import zipfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse

from ask.admin import _apply_zip_csv_metadata
from ask.admin_csv import validate_partial_date
from ask.models import (
    DocumentAuthorInstitution,
    DocumentType,
    InstitutionType,
    PDFResource,
)


class PDFResourceDeletionTests(TestCase):
    def setUp(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media_root)
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_user("curator", password="pw")

    def test_delete_removes_file_from_storage(self):
        pdf = PDFResource(title="Annual report", creator=self.user)
        pdf.file.save("report.pdf", ContentFile(b"%PDF-1.4 test"), save=True)
        storage, name = pdf.file.storage, pdf.file.name
        self.assertTrue(storage.exists(name))

        pdf.delete()

        self.assertFalse(storage.exists(name))

    def test_delete_without_file_does_not_error(self):
        pdf = PDFResource.objects.create(title="No file yet", creator=self.user)
        pdf.delete()
        self.assertFalse(PDFResource.objects.filter(pk=pdf.pk).exists())

    def test_failed_file_removal_is_flagged(self):
        pdf = PDFResource(title="Sensitive doc", creator=self.user)
        pdf.file.save("sensitive.pdf", ContentFile(b"%PDF-1.4 test"), save=True)
        with patch(
            "django.core.files.storage.FileSystemStorage.delete",
            side_effect=OSError("disk error"),
        ), self.assertLogs("ask.models", level="ERROR"):
            pdf.delete()
        self.assertTrue(pdf.file_deletion_failed)

    def test_successful_file_removal_is_not_flagged(self):
        pdf = PDFResource(title="Hospital report", creator=self.user)
        pdf.file.save("report.pdf", ContentFile(b"%PDF-1.4 test"), save=True)
        pdf.delete()
        self.assertFalse(pdf.file_deletion_failed)


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


class ApplyZipCsvMetadataTests(TestCase):
    def test_creates_lookups_and_sets_fields(self):
        obj = PDFResource(title="Doc")
        warnings = _apply_zip_csv_metadata(obj, {
            "date_published": "2023-06",
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
        warnings = _apply_zip_csv_metadata(obj, {"document_type": "  ", "date_published": ""})
        self.assertEqual(warnings, [])
        self.assertEqual(obj.date_published, "")
        self.assertIsNone(obj.document_type_id)
        self.assertEqual(_apply_zip_csv_metadata(PDFResource(title="Doc"), {}), [])

    def test_invalid_date_warns_and_leaves_field_blank(self):
        obj = PDFResource(title="Doc")
        warnings = _apply_zip_csv_metadata(obj, {"date_published": "not-a-date"})
        self.assertEqual(len(warnings), 1)
        self.assertIn("date_published", warnings[0])
        self.assertEqual(obj.date_published, "")


@override_settings(PDF_ZIP_CSV_COLUMNS=("filename", "title"))
class ZipUploadViewTests(TestCase):
    def setUp(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media_root)
        override.enable()
        self.addCleanup(override.disable)
        self.admin = User.objects.create_superuser("admin", "admin@example.com", "pw")
        self.client.force_login(self.admin)

    def _build_zip(self, csv_text, pdfs):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("metadata.csv", csv_text)
            for name, content in pdfs.items():
                archive.writestr(name, content)
        buf.seek(0)
        buf.name = "upload.zip"
        return buf

    def test_zip_import_applies_csv_metadata(self):
        csv_text = (
            "filename,title,date_published,document_type,"
            "document_author_institution,institution_type\r\n"
            "report.pdf,Annual Report,2022,Report,WHO,NGO\r\n"
        )
        zip_file = self._build_zip(csv_text, {"report.pdf": b"%PDF-1.4 test"})

        response = self.client.post(
            reverse("admin:ask_pdfresource_upload_zip"), {"zip_file": zip_file}
        )
        self.assertEqual(response.status_code, 302)

        pdf = PDFResource.objects.get(title="Annual Report")
        self.assertEqual(pdf.date_published, "2022")
        self.assertEqual(pdf.document_type.name, "Report")
        self.assertEqual(pdf.document_author_institution.name, "WHO")
        self.assertEqual(pdf.institution_type.name, "NGO")

    def test_zip_import_works_without_metadata_columns(self):
        csv_text = "filename,title\r\nreport.pdf,Plain Report\r\n"
        zip_file = self._build_zip(csv_text, {"report.pdf": b"%PDF-1.4 test"})

        response = self.client.post(
            reverse("admin:ask_pdfresource_upload_zip"), {"zip_file": zip_file}
        )
        self.assertEqual(response.status_code, 302)

        pdf = PDFResource.objects.get(title="Plain Report")
        self.assertEqual(pdf.date_published, "")
        self.assertIsNone(pdf.document_type_id)

    def test_zip_import_tolerates_whitespace_in_csv_header(self):
        # spaces after commas in the header row must not cause rows to be skipped
        csv_text = (
            "filename, title, date_published, document_type\r\n"
            "report.pdf,Spaced Report,2021,Report\r\n"
        )
        zip_file = self._build_zip(csv_text, {"report.pdf": b"%PDF-1.4 test"})

        response = self.client.post(
            reverse("admin:ask_pdfresource_upload_zip"), {"zip_file": zip_file}
        )
        self.assertEqual(response.status_code, 302)

        pdf = PDFResource.objects.get(title="Spaced Report")
        self.assertEqual(pdf.date_published, "2021")
        self.assertEqual(pdf.document_type.name, "Report")
