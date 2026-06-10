import datetime
import io
import json
import shutil
import tempfile
import zipfile
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import Permission, User
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse

from ask.admin import _apply_zip_csv_metadata
from ask.admin_csv import normalize_partial_date
from ask.models import (
    DocumentAuthorInstitution,
    DocumentType,
    InstitutionType,
    PDFResource,
    TermsAcceptance,
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


class NormalizePartialDateTests(TestCase):
    def test_full_date(self):
        self.assertEqual(normalize_partial_date("2024-03-15"), "2024-03-15")

    def test_year_month(self):
        self.assertEqual(normalize_partial_date("2024-03"), "2024-03")

    def test_year_only(self):
        self.assertEqual(normalize_partial_date("2024"), "2024")

    def test_blank_or_none_returns_empty(self):
        self.assertEqual(normalize_partial_date(""), "")
        self.assertEqual(normalize_partial_date("   "), "")
        self.assertEqual(normalize_partial_date(None), "")

    def test_impossible_calendar_dates_rejected(self):
        with self.assertRaises(ValueError):
            normalize_partial_date("2024-13")
        with self.assertRaises(ValueError):
            normalize_partial_date("2024-02-30")

    def test_non_iso_input_rejected(self):
        with self.assertRaises(ValueError):
            normalize_partial_date("March 2024")
        with self.assertRaises(ValueError):
            normalize_partial_date("24-03-15")


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

    def test_zip_update_file_overwrites_existing_pdf(self):
        csv_text = "filename,title\r\nreport.pdf,Annual Report\r\n"
        zip1 = self._build_zip(csv_text, {"report.pdf": b"%PDF-1.4 original"})
        self.client.post(reverse("admin:ask_pdfresource_upload_zip"), {"zip_file": zip1})

        zip2 = self._build_zip(csv_text, {"report.pdf": b"%PDF-1.4 updated"})
        response = self.client.post(
            reverse("admin:ask_pdfresource_upload_zip"),
            {"zip_file": zip2, "update_file": "on"},
        )
        self.assertEqual(response.status_code, 302)

        self.assertEqual(PDFResource.objects.count(), 1)
        pdf = PDFResource.objects.get(title="Annual Report")
        with pdf.file.open("rb") as f:
            self.assertEqual(f.read(), b"%PDF-1.4 updated")

    def test_zip_update_metadata_refreshes_fields(self):
        csv_text_1 = "filename,title\r\nreport.pdf,Annual Report\r\n"
        zip1 = self._build_zip(csv_text_1, {"report.pdf": b"%PDF-1.4 original"})
        self.client.post(reverse("admin:ask_pdfresource_upload_zip"), {"zip_file": zip1})

        pdf = PDFResource.objects.get(title="Annual Report")
        self.assertIsNone(pdf.date_published)
        self.assertIsNone(pdf.document_type_id)

        csv_text_2 = (
            "filename,title,date_published,document_type\r\n"
            "report.pdf,Annual Report,2024-03,Report\r\n"
        )
        # second zip's bytes must NOT replace the file when only update_metadata is on
        zip2 = self._build_zip(csv_text_2, {"report.pdf": b"%PDF-1.4 ignored"})
        response = self.client.post(
            reverse("admin:ask_pdfresource_upload_zip"),
            {"zip_file": zip2, "update_metadata": "on"},
        )
        self.assertEqual(response.status_code, 302)

        self.assertEqual(PDFResource.objects.count(), 1)
        pdf.refresh_from_db()
        self.assertEqual(pdf.date_published, datetime.date(2024, 3, 1))
        self.assertEqual(pdf.date_published_precision, "month")
        self.assertEqual(pdf.document_type.name, "Report")
        with pdf.file.open("rb") as f:
            self.assertEqual(f.read(), b"%PDF-1.4 original")

    def test_zip_no_update_flags_preserve_skip_behavior(self):
        csv_text = "filename,title\r\nreport.pdf,Annual Report\r\n"
        zip1 = self._build_zip(csv_text, {"report.pdf": b"%PDF-1.4 original"})
        self.client.post(reverse("admin:ask_pdfresource_upload_zip"), {"zip_file": zip1})

        zip2 = self._build_zip(csv_text, {"report.pdf": b"%PDF-1.4 updated"})
        response = self.client.post(
            reverse("admin:ask_pdfresource_upload_zip"), {"zip_file": zip2}
        )
        self.assertEqual(response.status_code, 302)

        self.assertEqual(PDFResource.objects.count(), 1)
        pdf = PDFResource.objects.get(title="Annual Report")
        with pdf.file.open("rb") as f:
            self.assertEqual(f.read(), b"%PDF-1.4 original")


class RunKbResourceUploadReplaceTests(TestCase):
    def setUp(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media_root)
        override.enable()
        self.addCleanup(override.disable)
        # the real run_kb_resource_upload closes DB connections in finally —
        # that kills the TestCase's wrapping transaction connection and breaks
        # later tests, so neutralize it for this class
        close_patcher = patch("ask.tasks.close_old_connections")
        close_patcher.start()
        self.addCleanup(close_patcher.stop)
        self.user = User.objects.create_user("curator", password="pw")

    def _make_pdf(self, mcp_id):
        obj = PDFResource(
            title="Annual Report",
            original_filename="report.pdf",
            creator=self.user,
            modifier=self.user,
            mcp_kb_document_id=mcp_id,
        )
        obj.file.save("report.pdf", ContentFile(b"%PDF-1.4 test"), save=True)
        return obj

    def test_replace_deletes_old_doc_then_re_adds(self):
        from ask import kb_connector
        from ask.tasks import run_kb_resource_upload

        obj = self._make_pdf(mcp_id=42)
        with patch.object(kb_connector, "delete_kb_document") as mock_del, patch.object(
            kb_connector, "add_pdf_to_kb", return_value={"doc_id": 99}
        ) as mock_add:
            run_kb_resource_upload("pdf", obj.pk, replace=True)

        mock_del.assert_called_once_with(42)
        mock_add.assert_called_once()
        obj.refresh_from_db()
        self.assertEqual(obj.mcp_kb_document_id, 99)
        self.assertEqual(obj.status, PDFResource.Status.SUCCESS)

    def test_replace_without_existing_doc_id_skips_delete(self):
        from ask import kb_connector
        from ask.tasks import run_kb_resource_upload

        obj = self._make_pdf(mcp_id=None)
        with patch.object(kb_connector, "delete_kb_document") as mock_del, patch.object(
            kb_connector, "add_pdf_to_kb", return_value={"doc_id": 99}
        ) as mock_add:
            run_kb_resource_upload("pdf", obj.pk, replace=True)

        mock_del.assert_not_called()
        mock_add.assert_called_once()
        obj.refresh_from_db()
        self.assertEqual(obj.mcp_kb_document_id, 99)

    def test_replace_swallows_delete_failure_and_still_adds(self):
        from ask import kb_connector
        from ask.tasks import run_kb_resource_upload

        obj = self._make_pdf(mcp_id=42)
        with patch.object(
            kb_connector, "delete_kb_document", side_effect=Exception("boom")
        ), patch.object(
            kb_connector, "add_pdf_to_kb", return_value={"doc_id": 99}
        ) as mock_add:
            run_kb_resource_upload("pdf", obj.pk, replace=True)

        mock_add.assert_called_once()
        obj.refresh_from_db()
        self.assertEqual(obj.mcp_kb_document_id, 99)


class KBAddPdfResourceViewTests(TestCase):
    """The Track-in-Hopper endpoint for untracked KB PDFs

    Verifies both branches: (a) KB serves the file back, in which case the
    new PDFResource has the bytes attached; (b) KB returns 404, in which
    case the row is created as tracking-only (file=None) — legacy KB docs
    ingested before local_path was recorded land in this branch.
    """

    URL = "/hopper/ask/kb/add-pdf-resource/"

    def setUp(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media_root)
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_user("curator", password="pw")
        self.user.user_permissions.add(
            Permission.objects.get(codename="add_pdfresource")
        )
        TermsAcceptance.objects.create(
            user=self.user, terms_version=settings.TERMS_VERSION
        )
        self.client.force_login(self.user)

    def _post(self, body=None, raw=None):
        payload = raw if raw is not None else json.dumps(body or {})
        return self.client.post(self.URL, data=payload, content_type="application/json")

    @patch("ask.views.download_kb_pdf")
    def test_attaches_file_when_kb_returns_bytes(self, mock_download):
        mock_download.return_value = ("1780-report.pdf", b"%PDF-1.4 fake")
        resp = self._post({"doc_id": 99, "title": "Fresh doc"})
        self.assertEqual(resp.status_code, 200)
        pdf = PDFResource.objects.get(pk=resp.json()["id"])
        self.assertEqual(pdf.mcp_kb_document_id, 99)
        self.assertTrue(pdf.file)
        self.assertEqual(pdf.file.read(), b"%PDF-1.4 fake")
        self.assertEqual(pdf.original_filename, "1780-report.pdf")
        self.assertEqual(pdf.status_message, "")

    @patch("ask.views.download_kb_pdf")
    def test_creates_tracking_only_when_kb_has_no_file(self, mock_download):
        mock_download.return_value = (None, None)
        resp = self._post({"doc_id": 42, "title": "Legacy doc"})
        self.assertEqual(resp.status_code, 200)
        pdf = PDFResource.objects.get(pk=resp.json()["id"])
        self.assertEqual(pdf.mcp_kb_document_id, 42)
        self.assertFalse(pdf.file)
        self.assertEqual(
            pdf.status_message, "Tracked from KB; file not stored locally."
        )

    @patch("ask.views.download_kb_pdf")
    def test_blank_title_falls_back_to_placeholder(self, mock_download):
        mock_download.return_value = (None, None)
        resp = self._post({"doc_id": 7})
        self.assertEqual(resp.status_code, 200)
        pdf = PDFResource.objects.get(pk=resp.json()["id"])
        self.assertEqual(pdf.title, "Untitled KB doc 7")

    @patch("ask.views.download_kb_pdf")
    def test_duplicate_doc_id_refused(self, mock_download):
        mock_download.return_value = (None, None)
        first = self._post({"doc_id": 42, "title": "first"})
        self.assertEqual(first.status_code, 200)
        second = self._post({"doc_id": 42, "title": "second"})
        self.assertEqual(second.status_code, 400)
        self.assertIn("Already tracked", second.json()["error"])
        self.assertEqual(PDFResource.objects.filter(mcp_kb_document_id=42).count(), 1)

    def test_missing_doc_id_rejected(self):
        resp = self._post({"title": "no id"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("doc_id", resp.json()["error"])

    def test_non_integer_doc_id_rejected(self):
        resp = self._post({"doc_id": "not-an-int", "title": "x"})
        self.assertEqual(resp.status_code, 400)

    def test_malformed_json_rejected(self):
        resp = self._post(raw="not json")
        self.assertEqual(resp.status_code, 400)

    def test_permission_required(self):
        noperm = User.objects.create_user("viewer", password="pw")
        TermsAcceptance.objects.create(
            user=noperm, terms_version=settings.TERMS_VERSION
        )
        self.client.force_login(noperm)
        resp = self._post({"doc_id": 42, "title": "x"})
        self.assertEqual(resp.status_code, 403)

    @patch("ask.views.download_kb_pdf")
    def test_response_payload_powers_the_dom_injection(self, mock_download):
        # The frontend needs id, title, and filename to render the new row
        # without a full page reload
        mock_download.return_value = ("1780-foo.pdf", b"bytes")
        resp = self._post({"doc_id": 11, "title": "Fresh"})
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["title"], "Fresh")
        self.assertTrue(body["filename"])
        self.assertIn("id", body)


class DownloadKBPdfHelperTests(TestCase):
    """Unit tests for the new kb_connector.download_kb_pdf helper."""

    def _stub_response(self, *, status_code, content=b"", headers=None):
        resp = type("Resp", (), {})()
        resp.status_code = status_code
        resp.content = content
        resp.headers = headers or {}
        resp.raise_for_status = lambda: None
        return resp

    @patch("ask.kb_connector.httpx.Client")
    def test_returns_filename_and_bytes_on_200(self, mock_client_cls):
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.get.return_value = self._stub_response(
            status_code=200,
            content=b"%PDF fake",
            headers={"content-disposition": 'attachment; filename="1780-foo.pdf"'},
        )
        from ask.kb_connector import download_kb_pdf
        self.assertEqual(download_kb_pdf(5), ("1780-foo.pdf", b"%PDF fake"))

    @patch("ask.kb_connector.httpx.Client")
    def test_returns_none_pair_on_404(self, mock_client_cls):
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.get.return_value = self._stub_response(status_code=404)
        from ask.kb_connector import download_kb_pdf
        self.assertEqual(download_kb_pdf(99), (None, None))

    @patch("ask.kb_connector.httpx.Client")
    def test_falls_back_to_synthetic_filename_when_header_missing(self, mock_client_cls):
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.get.return_value = self._stub_response(
            status_code=200, content=b"bytes"
        )
        from ask.kb_connector import download_kb_pdf
        fname, content = download_kb_pdf(7)
        self.assertEqual(fname, "kb_doc_7.pdf")
        self.assertEqual(content, b"bytes")
