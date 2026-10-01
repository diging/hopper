import json
import shutil
import tempfile
from unittest.mock import patch

import httpx
from django.conf import settings
from django.contrib.auth.models import Permission, User
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from ask.models import PDFResource, TermsAcceptance, WebsiteResource
from ask.tests.utils import accept_terms, http_status_error


class KBResourcesViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("viewer", password="pw")
        accept_terms(self.user)
        self.client.force_login(self.user)

    def test_lists_resources_and_reports_permissions(self):
        WebsiteResource.objects.create(title="Site", url="https://example.com", creator=self.user)
        resp = self.client.get(reverse("ask:kb-resources"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["web_page_obj"]), 1)
        self.assertFalse(resp.context["can_add"])
        self.assertFalse(resp.context["can_delete_pdf"])

    def test_curator_permissions_exposed(self):
        self.user.user_permissions.add(Permission.objects.get(codename="add_websiteresource"))
        resp = self.client.get(reverse("ask:kb-resources"))
        self.assertTrue(resp.context["can_add"])


class KBCompareViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("viewer", password="pw")
        accept_terms(self.user)
        self.client.force_login(self.user)

    def _post(self):
        return self.client.post(reverse("ask:kb-compare"))

    @patch("ask.views.list_kb_documents")
    def test_classifies_tracked_missing_and_untracked(self, mock_list):
        WebsiteResource.objects.create(title="In", url="https://in.test", creator=self.user)
        WebsiteResource.objects.create(title="Out", url="https://out.test", creator=self.user)
        PDFResource.objects.create(title="PDF in", creator=self.user, mcp_kb_document_id=3)
        PDFResource.objects.create(title="PDF out", creator=self.user)
        # two pages from the KB, to exercise pagination
        mock_list.side_effect = [
            {"total": 4, "documents": [
                {"id": 1, "title": "In", "url": "https://in.test"},
                {"id": 2, "title": "Extra", "url": "https://extra.test"},
            ]},
            {"total": 4, "documents": [
                {"id": 3, "title": "PDF in", "doc_type": "pdf"},
                {"id": 4, "title": "PDF extra", "doc_type": "pdf"},
            ]},
        ]

        body = self._post().json()

        self.assertEqual(mock_list.call_count, 2)
        statuses = {r["url"]: r["status"] for r in body["resources"]}
        self.assertEqual(statuses, {"https://in.test": "in_kb", "https://out.test": "missing_from_kb"})
        self.assertEqual(body["untracked"], [
            {"url": "https://extra.test", "title": "Extra", "doc_id": 2}
        ])
        pdf_statuses = {r["title"]: r["status"] for r in body["pdf_results"]}
        self.assertEqual(pdf_statuses, {"PDF in": "in_kb", "PDF out": "missing_from_kb"})
        self.assertEqual(body["untracked_pdfs"], [{"title": "PDF extra", "doc_id": 4}])
        self.assertEqual(body["kb_total"], 4)
        self.assertEqual(body["internal_total"], 4)

    @patch("ask.views.list_kb_documents", side_effect=httpx.ConnectError("down"))
    def test_connection_error_returns_503(self, _):
        resp = self._post()
        self.assertEqual(resp.status_code, 503)
        self.assertFalse(resp.json()["success"])

    @patch("ask.views.list_kb_documents", side_effect=http_status_error(500))
    def test_kb_http_error_returns_502(self, _):
        self.assertEqual(self._post().status_code, 502)

    @patch("ask.views.list_kb_documents", side_effect=RuntimeError("bug"))
    def test_unexpected_error_returns_500(self, _):
        with self.assertLogs("ask.views", level="ERROR"):
            self.assertEqual(self._post().status_code, 500)


class KBAddResourceViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("curator", password="pw")
        self.user.user_permissions.add(Permission.objects.get(codename="add_websiteresource"))
        accept_terms(self.user)
        self.client.force_login(self.user)

    def _post(self, body=None, raw=None):
        payload = raw if raw is not None else json.dumps(body or {})
        return self.client.post(
            reverse("ask:kb-add-resource"), data=payload, content_type="application/json"
        )

    def test_creates_resource(self):
        resp = self._post({"url": "https://example.com", "title": "Example", "doc_id": "5"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["merged"])
        resource = WebsiteResource.objects.get(pk=resp.json()["id"])
        self.assertEqual(resource.title, "Example")
        self.assertEqual(resource.mcp_kb_document_id, 5)
        self.assertEqual(resource.creator, self.user)

    def test_blank_title_falls_back_to_url(self):
        resp = self._post({"url": "https://example.com"})
        resource = WebsiteResource.objects.get(pk=resp.json()["id"])
        self.assertEqual(resource.title, "https://example.com")
        self.assertIsNone(resource.mcp_kb_document_id)

    def test_existing_url_is_merged(self):
        existing = WebsiteResource.objects.create(
            title="Existing", url="https://example.com", creator=self.user
        )
        resp = self._post({"url": "https://example.com", "title": "New", "doc_id": 7})
        self.assertEqual(resp.json(), {"success": True, "id": existing.id, "merged": True})
        existing.refresh_from_db()
        self.assertEqual(existing.mcp_kb_document_id, 7)
        self.assertEqual(existing.title, "Existing")
        self.assertEqual(WebsiteResource.objects.count(), 1)

    def test_missing_url_rejected(self):
        self.assertEqual(self._post({"title": "x"}).status_code, 400)

    def test_malformed_json_rejected(self):
        self.assertEqual(self._post(raw="nope").status_code, 400)

    def test_permission_required(self):
        self.user.user_permissions.clear()
        self.assertEqual(self._post({"url": "https://example.com"}).status_code, 403)
        self.assertFalse(WebsiteResource.objects.exists())

    def test_non_integer_doc_id_is_ignored(self):
        resp = self._post({"url": "https://example.com", "doc_id": "abc"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(WebsiteResource.objects.get(pk=resp.json()["id"]).mcp_kb_document_id)


class KBRemoveFromKBViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("curator", password="pw")
        self.user.user_permissions.add(Permission.objects.get(codename="delete_websiteresource"))
        accept_terms(self.user)
        self.client.force_login(self.user)

    def _post(self, body):
        return self.client.post(
            reverse("ask:kb-remove-from-kb"), data=json.dumps(body), content_type="application/json"
        )

    @patch("ask.views.delete_kb_document")
    def test_deletes_document(self, mock_delete):
        resp = self._post({"doc_id": 12})
        self.assertEqual(resp.status_code, 200)
        mock_delete.assert_called_once_with(12)

    @patch("ask.views.delete_kb_document")
    def test_missing_doc_id_rejected(self, mock_delete):
        self.assertEqual(self._post({}).status_code, 400)
        mock_delete.assert_not_called()

    @patch("ask.views.delete_kb_document", side_effect=httpx.ConnectError("down"))
    def test_connection_error_returns_503(self, _):
        self.assertEqual(self._post({"doc_id": 12}).status_code, 503)

    @patch("ask.views.delete_kb_document")
    def test_permission_required(self, mock_delete):
        self.user.user_permissions.clear()
        self.assertEqual(self._post({"doc_id": 12}).status_code, 403)
        mock_delete.assert_not_called()

    def test_malformed_json_rejected(self):
        resp = self.client.post(
            reverse("ask:kb-remove-from-kb"), data="nope", content_type="application/json"
        )
        self.assertEqual(resp.status_code, 400)

    @patch("ask.views.delete_kb_document", side_effect=http_status_error(500))
    def test_kb_http_error_returns_502(self, _):
        self.assertEqual(self._post({"doc_id": 12}).status_code, 502)


class KBAddWebsiteToMcpViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("curator", password="pw")
        self.user.user_permissions.add(Permission.objects.get(codename="change_websiteresource"))
        accept_terms(self.user)
        self.client.force_login(self.user)
        self.resource = WebsiteResource.objects.create(
            title="Site", url="https://example.com", creator=self.user
        )

    def _post(self, body):
        return self.client.post(
            reverse("ask:kb-add-to-kb"), data=json.dumps(body), content_type="application/json"
        )

    @patch("ask.views.add_website_to_kb", return_value={"doc_id": 77})
    def test_reingests_and_stores_doc_id(self, mock_add):
        resp = self._post({"id": self.resource.id})
        self.assertEqual(resp.json(), {"success": True, "doc_id": 77})
        mock_add.assert_called_once_with("https://example.com")
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.mcp_kb_document_id, 77)
        self.assertEqual(self.resource.modifier, self.user)

    def test_missing_id_rejected(self):
        self.assertEqual(self._post({}).status_code, 400)

    def test_unknown_resource_returns_404(self):
        self.assertEqual(self._post({"id": 999999}).status_code, 404)

    @patch("ask.views.add_website_to_kb", side_effect=http_status_error(500))
    def test_kb_http_error_returns_502(self, _):
        self.assertEqual(self._post({"id": self.resource.id}).status_code, 502)
        self.resource.refresh_from_db()
        self.assertIsNone(self.resource.mcp_kb_document_id)

    def test_permission_required(self):
        self.user.user_permissions.clear()
        self.assertEqual(self._post({"id": self.resource.id}).status_code, 403)

    def test_malformed_json_rejected(self):
        resp = self.client.post(
            reverse("ask:kb-add-to-kb"), data="nope", content_type="application/json"
        )
        self.assertEqual(resp.status_code, 400)

    @patch("ask.views.add_website_to_kb", side_effect=httpx.ConnectError("down"))
    def test_connection_error_returns_503(self, _):
        self.assertEqual(self._post({"id": self.resource.id}).status_code, 503)


class KBUploadPdfViewTests(TestCase):
    def setUp(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media_root)
        override.enable()
        self.addCleanup(override.disable)

        self.user = User.objects.create_user("curator", password="pw")
        self.user.user_permissions.add(Permission.objects.get(codename="add_pdfresource"))
        accept_terms(self.user)
        self.client.force_login(self.user)

    def _post(self, data):
        return self.client.post(reverse("ask:kb-upload-pdf"), data)

    def _pdf(self, name="report.pdf", content=b"%PDF-1.4 test"):
        return SimpleUploadedFile(name, content, content_type="application/pdf")

    @patch("ask.views.add_pdf_to_kb", return_value={"doc_id": 55})
    def test_saves_locally_and_sends_to_kb(self, mock_add):
        resp = self._post({"title": "Report", "file": self._pdf()})
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["doc_id"], 55)
        pdf = PDFResource.objects.get(pk=body["id"])
        self.assertEqual(pdf.mcp_kb_document_id, 55)
        self.assertEqual(pdf.file.read(), b"%PDF-1.4 test")
        mock_add.assert_called_once_with(b"%PDF-1.4 test", "report.pdf", "Report")

    @patch("ask.views.add_pdf_to_kb", side_effect=httpx.ConnectError("down"))
    def test_kb_failure_keeps_local_copy_with_warning(self, _):
        with self.assertLogs("ask.views", level="ERROR"):
            resp = self._post({"title": "Report", "file": self._pdf()})
        body = resp.json()
        self.assertFalse(body["success"])
        self.assertIn("warning", body)
        self.assertIsNone(PDFResource.objects.get(pk=body["id"]).mcp_kb_document_id)

    def test_missing_file_rejected(self):
        self.assertEqual(self._post({"title": "Report"}).status_code, 400)

    def test_missing_title_rejected(self):
        self.assertEqual(self._post({"file": self._pdf()}).status_code, 400)

    def test_non_pdf_rejected(self):
        resp = self._post({"title": "Report", "file": self._pdf(name="notes.txt")})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(PDFResource.objects.exists())

    @override_settings(KB_PDF_MAX_SIZE_MB=0)
    def test_oversized_file_rejected(self):
        resp = self._post({"title": "Report", "file": self._pdf()})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(PDFResource.objects.exists())

    def test_permission_required(self):
        self.user.user_permissions.clear()
        resp = self._post({"title": "Report", "file": self._pdf()})
        self.assertEqual(resp.status_code, 403)


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
    def test_duplicate_doc_id_is_merged(self, mock_download):
        mock_download.return_value = (None, None)
        first = self._post({"doc_id": 42, "title": "first"})
        self.assertEqual(first.status_code, 200)
        second = self._post({"doc_id": 42, "title": "second"})
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["merged"])
        self.assertEqual(second.json()["id"], first.json()["id"])
        self.assertEqual(PDFResource.objects.filter(mcp_kb_document_id=42).count(), 1)
        mock_download.assert_called_once()

    @patch("ask.views.download_kb_pdf")
    def test_unlinked_row_with_matching_title_is_linked(self, mock_download):
        # e.g. a zip upload that never reached the KB: link it rather than download a copy
        local = PDFResource.objects.create(
            title="Annual report", creator=self.user, status=PDFResource.Status.ERROR
        )
        resp = self._post({"doc_id": 55, "title": "Annual report"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["merged"])
        self.assertEqual(resp.json()["id"], local.id)
        local.refresh_from_db()
        self.assertEqual(local.mcp_kb_document_id, 55)
        self.assertEqual(local.modifier, self.user)
        self.assertEqual(local.status, PDFResource.Status.SUCCESS)
        self.assertEqual(PDFResource.objects.count(), 1)
        mock_download.assert_not_called()

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

    @patch("ask.views.download_kb_pdf", side_effect=httpx.ConnectError("down"))
    def test_connection_error_returns_503(self, _):
        resp = self._post({"doc_id": 42, "title": "x"})
        self.assertEqual(resp.status_code, 503)
        self.assertFalse(PDFResource.objects.exists())

    @patch("ask.views.download_kb_pdf", side_effect=http_status_error(500))
    def test_kb_http_error_returns_502(self, _):
        resp = self._post({"doc_id": 42, "title": "x"})
        self.assertEqual(resp.status_code, 502)
        self.assertIn("HTTP 500", resp.json()["error"])
        self.assertFalse(PDFResource.objects.exists())


class KBAddPdfToMcpViewTests(TestCase):
    """The re-ingest endpoint that pushes a stored PDF back into the KB.

    A PDFResource can legitimately exist with no local file (tracking-only
    rows created when the KB doc's bytes were never downloaded). Re-ingesting
    such a row has no bytes to send, so it must fail cleanly instead of
    raising ValueError from the empty FileField.
    """

    URL = "/hopper/ask/kb/add-pdf-to-kb/"

    def setUp(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media_root)
        override.enable()
        self.addCleanup(override.disable)

        self.user = User.objects.create_user("curator", password="pw")
        self.user.user_permissions.add(
            Permission.objects.get(codename="change_pdfresource")
        )
        TermsAcceptance.objects.create(
            user=self.user, terms_version=settings.TERMS_VERSION
        )
        self.client.force_login(self.user)

    def _post(self, body):
        return self.client.post(
            self.URL, data=json.dumps(body), content_type="application/json"
        )

    @patch("ask.views.add_pdf_to_kb")
    def test_resource_without_file_fails_cleanly(self, mock_add):
        pdf = PDFResource.objects.create(title="Tracking only", creator=self.user)
        resp = self._post({"id": pdf.id})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["success"])
        mock_add.assert_not_called()

    @patch("ask.views.add_pdf_to_kb")
    def test_resource_with_file_is_reingested(self, mock_add):
        mock_add.return_value = {"doc_id": 321}
        pdf = PDFResource(title="Real report", creator=self.user)
        pdf.file.save("report.pdf", ContentFile(b"%PDF-1.4 real"), save=True)
        resp = self._post({"id": pdf.id})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        pdf.refresh_from_db()
        self.assertEqual(pdf.mcp_kb_document_id, 321)

    def _pdf_with_file(self, **fields):
        pdf = PDFResource(title="Real report", creator=self.user, **fields)
        pdf.file.save("report.pdf", ContentFile(b"%PDF-1.4 real"), save=True)
        return pdf

    @patch("ask.views.add_pdf_to_kb")
    @patch("ask.views.update_pdf_in_kb")
    def test_linked_resource_updates_existing_kb_document(self, mock_update, mock_add):
        mock_update.return_value = {"doc_id": 321}
        pdf = self._pdf_with_file(mcp_kb_document_id=321)
        resp = self._post({"id": pdf.id})
        self.assertEqual(resp.json(), {"success": True, "doc_id": 321})
        mock_update.assert_called_once_with(
            321, b"%PDF-1.4 real", pdf.file.name.split("/")[-1], "Real report"
        )
        mock_add.assert_not_called()
        pdf.refresh_from_db()
        self.assertEqual(pdf.modifier, self.user)

    def test_missing_id_rejected(self):
        self.assertEqual(self._post({}).status_code, 400)

    def test_unknown_resource_returns_404(self):
        self.assertEqual(self._post({"id": 999999}).status_code, 404)

    def test_malformed_json_rejected(self):
        resp = self.client.post(self.URL, data="nope", content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    @patch("ask.views.add_pdf_to_kb")
    def test_permission_required(self, mock_add):
        self.user.user_permissions.clear()
        pdf = self._pdf_with_file()
        self.assertEqual(self._post({"id": pdf.id}).status_code, 403)
        mock_add.assert_not_called()

    @patch("ask.views.add_pdf_to_kb", side_effect=httpx.ConnectError("down"))
    def test_connection_error_returns_503(self, _):
        pdf = self._pdf_with_file()
        self.assertEqual(self._post({"id": pdf.id}).status_code, 503)
        pdf.refresh_from_db()
        self.assertIsNone(pdf.mcp_kb_document_id)

    @patch("ask.views.add_pdf_to_kb")
    def test_kb_error_message_is_passed_through(self, mock_add):
        request = httpx.Request("POST", "http://kb.test/docs/pdf/add")
        mock_add.side_effect = httpx.HTTPStatusError(
            "error", request=request,
            response=httpx.Response(422, json={"error": "PDF has no extractable text."}, request=request),
        )
        resp = self._post({"id": self._pdf_with_file().id})
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json()["error"], "PDF has no extractable text.")

    @patch("ask.views.add_pdf_to_kb", side_effect=http_status_error(500))
    def test_kb_error_without_json_body_gets_generic_message(self, _):
        resp = self._post({"id": self._pdf_with_file().id})
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json()["error"], "KB server error (HTTP 500).")


class GetPdfViewTests(TestCase):
    def setUp(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media_root)
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_user("alice", password="pw")
        accept_terms(self.user)
        self.client.force_login(self.user)
        pdf = PDFResource(title="Report", creator=self.user)
        pdf.file.save("report.pdf", ContentFile(b"%PDF-1.4 served"), save=True)
        self.url = reverse("get_pdf", kwargs={"filename": pdf.file.name.split("/")[-1]})

    def test_serves_pdf_inline(self):
        resp = self.client.get(self.url)
        self.addCleanup(resp.close)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b"".join(resp.streaming_content), b"%PDF-1.4 served")
        self.assertTrue(resp["Content-Disposition"].startswith("inline"))
        self.assertEqual(resp["Content-Type"], "application/pdf")

    def test_requires_login(self):
        self.client.logout()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.url.startswith(settings.LOGIN_URL))
