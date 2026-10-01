import shutil
import tempfile
from unittest.mock import patch

import httpx
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from ask.models import DocumentType, PDFResource, WebsiteResource
from ask.tasks import run_kb_resource_upload


@patch("ask.tasks.close_old_connections")
class RunKBResourceUploadTests(TestCase):
    def setUp(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media_root)
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_user("curator", password="pw")

    def _website(self):
        return WebsiteResource.objects.create(
            title="Site", url="https://example.com", creator=self.user,
            date_published="2024", document_type=DocumentType.objects.create(name="Report"),
            status=WebsiteResource.Status.PROCESSING,
        )

    @patch("ask.kb_connector.add_website_to_kb", return_value={"doc_id": 8})
    def test_website_success_records_doc_id_and_metadata(self, mock_add, _):
        resource = self._website()
        run_kb_resource_upload("website", resource.id)
        mock_add.assert_called_once_with("https://example.com", metadata={
            "date_published": "2024",
            "document_type": "Report",
            "publisher": None,
            "document_author_institution": None,
            "institution_type": None,
        })
        resource.refresh_from_db()
        self.assertEqual(resource.mcp_kb_document_id, 8)
        self.assertEqual(resource.status, WebsiteResource.Status.SUCCESS)

    @patch("ask.kb_connector.add_website_to_kb", side_effect=httpx.ReadTimeout("slow"))
    def test_timeout_is_a_warning(self, _mock_add, _):
        resource = self._website()
        with self.assertLogs("ask.tasks", level="ERROR"):
            run_kb_resource_upload("website", resource.id)
        resource.refresh_from_db()
        self.assertEqual(resource.status, WebsiteResource.Status.WARNING)
        self.assertIn("Do not re-upload", resource.status_message)

    @patch("ask.kb_connector.add_pdf_to_kb", side_effect=RuntimeError("rejected"))
    def test_pdf_failure_marks_error_and_removes_file(self, _mock_add, _):
        pdf = PDFResource(title="Report", creator=self.user)
        pdf.file.save("report.pdf", ContentFile(b"%PDF-1.4"), save=True)
        storage, name = pdf.file.storage, pdf.file.name

        with self.assertLogs("ask.tasks", level="ERROR"):
            run_kb_resource_upload("pdf", pdf.id)

        pdf.refresh_from_db()
        self.assertEqual(pdf.status, PDFResource.Status.ERROR)
        self.assertIn("rejected", pdf.status_message)
        self.assertFalse(pdf.file)
        self.assertFalse(storage.exists(name))

    def test_unknown_label_or_missing_row_is_logged(self, _):
        with self.assertLogs("ask.tasks", level="ERROR"):
            run_kb_resource_upload("video", 1)
        with self.assertLogs("ask.tasks", level="ERROR"):
            run_kb_resource_upload("website", 999999)
