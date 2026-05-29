import json
import shutil
import tempfile
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import Permission, User
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from ask.models import PDFResource, TermsAcceptance


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
        mock_download.return_value = None
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
        mock_download.return_value = None
        resp = self._post({"doc_id": 7})
        self.assertEqual(resp.status_code, 200)
        pdf = PDFResource.objects.get(pk=resp.json()["id"])
        self.assertEqual(pdf.title, "Untitled KB doc 7")

    @patch("ask.views.download_kb_pdf")
    def test_duplicate_doc_id_refused(self, mock_download):
        mock_download.return_value = None
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
    def test_returns_none_on_404(self, mock_client_cls):
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.get.return_value = self._stub_response(status_code=404)
        from ask.kb_connector import download_kb_pdf
        self.assertIsNone(download_kb_pdf(99))

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
