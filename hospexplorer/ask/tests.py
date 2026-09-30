import io
import json
import shutil
import tempfile
import uuid
import zipfile
from unittest.mock import patch

import httpx
from django.conf import settings
from django.contrib.auth.models import AnonymousUser, Permission, User
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from ask.admin import _apply_zip_csv_metadata
from ask.admin_csv import validate_partial_date
from ask.context_processors import sidebar_conversations, terms_status
from ask.llm_connector import query_llm
from ask.models import (
    Conversation,
    DocumentAuthorInstitution,
    DocumentType,
    InstitutionType,
    PDFResource,
    QARecord,
    QueryTask,
    SimWorkflow,
    TermsAcceptance,
    WebsiteResource,
)
from ask.tasks import (
    _enrich_search_results,
    _normalize_doc_id,
    run_kb_resource_upload,
    run_llm_task,
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
            "filename,title,Date Published,document_type,"
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
            "filename, title, Date Published, document_type\r\n"
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


def _accept_terms(user):
    TermsAcceptance.objects.create(user=user, terms_version=settings.TERMS_VERSION)


class ConversationViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", password="pw")
        _accept_terms(self.user)
        self.client.force_login(self.user)

    def test_index_requires_login(self):
        self.client.logout()
        resp = self.client.get(reverse("ask:index"))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.url.startswith(settings.LOGIN_URL))

    def test_index_renders(self):
        resp = self.client.get(reverse("ask:index"))
        self.assertEqual(resp.status_code, 200)

    def test_new_conversation_creates_and_redirects(self):
        resp = self.client.post(reverse("ask:new-conversation"))
        conversation = Conversation.objects.get(user=self.user)
        self.assertRedirects(
            resp, reverse("ask:conversation", kwargs={"conversation_id": conversation.id})
        )

    def test_new_conversation_rejects_get(self):
        resp = self.client.get(reverse("ask:new-conversation"))
        self.assertEqual(resp.status_code, 405)
        self.assertFalse(Conversation.objects.exists())

    def test_conversation_detail_shows_own_conversation(self):
        conversation = Conversation.objects.create(user=self.user, title="Mine")
        resp = self.client.get(
            reverse("ask:conversation", kwargs={"conversation_id": conversation.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["conversation"], conversation)

    def test_conversation_detail_hides_other_users_conversation(self):
        other = User.objects.create_user("bob", password="pw")
        conversation = Conversation.objects.create(user=other, title="Not yours")
        resp = self.client.get(
            reverse("ask:conversation", kwargs={"conversation_id": conversation.id})
        )
        self.assertRedirects(resp, reverse("ask:index"))


@patch("ask.views.threading.Thread")
class QueryViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", password="pw")
        _accept_terms(self.user)
        self.client.force_login(self.user)

    def _post(self, body=None, raw=None):
        payload = raw if raw is not None else json.dumps(body or {})
        return self.client.post(
            reverse("ask:query-llm"), data=payload, content_type="application/json"
        )

    def test_malformed_json_rejected(self, mock_thread):
        resp = self._post(raw="not json")
        self.assertEqual(resp.status_code, 400)
        mock_thread.assert_not_called()

    def test_blank_query_rejected(self, mock_thread):
        resp = self._post({"query": "   "})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(QueryTask.objects.exists())
        mock_thread.assert_not_called()

    def test_get_not_allowed(self, mock_thread):
        resp = self.client.get(reverse("ask:query-llm"))
        self.assertEqual(resp.status_code, 405)

    def test_creates_records_and_starts_background_task(self, mock_thread):
        resp = self._post({"query": "  How many beds?  "})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        task = QueryTask.objects.get(pk=body["task_id"])
        self.assertEqual(task.user, self.user)
        self.assertEqual(task.query_text, "How many beds?")
        self.assertEqual(task.status, QueryTask.Status.PENDING)

        conversation = Conversation.objects.get(pk=body["conversation_id"])
        self.assertEqual(conversation.user, self.user)
        self.assertEqual(conversation.title, "How many beds?")
        self.assertEqual(body["conversation_title"], "How many beds?")

        record = QARecord.objects.get(conversation=conversation)
        self.assertEqual(record.question_text, "How many beds?")

        mock_thread.assert_called_once_with(
            target=run_llm_task, args=(task.id, record.id, conversation.id), daemon=True
        )
        mock_thread.return_value.start.assert_called_once()

    def test_uses_given_conversation_and_keeps_existing_title(self, mock_thread):
        conversation = Conversation.objects.create(user=self.user, title="Original")
        resp = self._post({"query": "Follow-up", "conversation_id": conversation.id})
        self.assertEqual(resp.json()["conversation_id"], conversation.id)
        conversation.refresh_from_db()
        self.assertEqual(conversation.title, "Original")
        self.assertEqual(conversation.qa_records.count(), 1)

    def test_falls_back_to_most_recent_conversation(self, mock_thread):
        Conversation.objects.create(user=self.user, title="Older")
        latest = Conversation.objects.create(user=self.user, title="Latest")
        resp = self._post({"query": "No id given"})
        self.assertEqual(resp.json()["conversation_id"], latest.id)
        self.assertEqual(Conversation.objects.filter(user=self.user).count(), 2)

    def test_title_truncated_to_200_chars(self, mock_thread):
        resp = self._post({"query": "x" * 300})
        conversation = Conversation.objects.get(pk=resp.json()["conversation_id"])
        self.assertEqual(len(conversation.title), 200)

    def test_other_users_conversation_returns_404(self, mock_thread):
        other = User.objects.create_user("bob", password="pw")
        conversation = Conversation.objects.create(user=other)
        resp = self._post({"query": "sneaky", "conversation_id": conversation.id})
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(QARecord.objects.exists())
        mock_thread.assert_not_called()


class PollQueryViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", password="pw")
        _accept_terms(self.user)
        self.client.force_login(self.user)

    def _poll(self, task):
        return self.client.get(reverse("ask:poll-query", kwargs={"task_id": task.id}))

    def test_pending_task_reports_status_only(self):
        task = QueryTask.objects.create(user=self.user, query_text="q")
        resp = self._poll(task)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "pending"})

    def test_completed_task_returns_state_results_first(self):
        result = {"search_results": [
            {"title": "federal", "publisher": "Federal"},
            {"title": "state", "publisher": "State"},
            {"title": "none"},
        ]}
        task = QueryTask.objects.create(
            user=self.user, query_text="q",
            status=QueryTask.Status.COMPLETED, result=json.dumps(result),
        )
        body = self._poll(task).json()
        self.assertEqual(body["status"], "completed")
        titles = [r["title"] for r in json.loads(body["message"])["search_results"]]
        self.assertEqual(titles, ["state", "federal", "none"])

    def test_failed_task_returns_error(self):
        task = QueryTask.objects.create(
            user=self.user, query_text="q",
            status=QueryTask.Status.FAILED, error_message="boom",
        )
        body = self._poll(task).json()
        self.assertEqual(body, {"status": "failed", "error": "boom"})

    def test_other_users_task_returns_404(self):
        other = User.objects.create_user("bob", password="pw")
        task = QueryTask.objects.create(user=other, query_text="q")
        self.assertEqual(self._poll(task).status_code, 404)

    def test_post_not_allowed(self):
        task = QueryTask.objects.create(user=self.user, query_text="q")
        resp = self.client.post(reverse("ask:poll-query", kwargs={"task_id": task.id}))
        self.assertEqual(resp.status_code, 405)


class DeleteHistoryViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", password="pw")
        _accept_terms(self.user)
        self.client.force_login(self.user)

    def test_deletes_only_own_conversations(self):
        mine = Conversation.objects.create(user=self.user)
        QARecord.objects.create(conversation=mine, user=self.user, question_text="q")
        other = User.objects.create_user("bob", password="pw")
        theirs = Conversation.objects.create(user=other)

        resp = self.client.delete(reverse("ask:delete-history"))

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Conversation.objects.filter(user=self.user).exists())
        self.assertFalse(QARecord.objects.filter(user=self.user).exists())
        self.assertTrue(Conversation.objects.filter(pk=theirs.pk).exists())

    def test_post_not_allowed(self):
        Conversation.objects.create(user=self.user)
        resp = self.client.post(reverse("ask:delete-history"))
        self.assertEqual(resp.status_code, 405)
        self.assertTrue(Conversation.objects.filter(user=self.user).exists())


class TermsAcceptanceMiddlewareTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", password="pw")
        self.client.force_login(self.user)

    def test_user_without_acceptance_is_redirected(self):
        resp = self.client.get(reverse("ask:index"))
        self.assertRedirects(resp, reverse("ask:terms-accept"))

    def test_outdated_acceptance_is_redirected(self):
        TermsAcceptance.objects.create(user=self.user, terms_version="old")
        resp = self.client.get(reverse("ask:index"))
        self.assertRedirects(resp, reverse("ask:terms-accept"))

    def test_accepted_user_passes_and_is_cached_in_session(self):
        _accept_terms(self.user)
        resp = self.client.get(reverse("ask:index"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.session["terms_accepted_version"], settings.TERMS_VERSION)

        # the session cache means the DB row is no longer consulted
        TermsAcceptance.objects.all().delete()
        self.assertEqual(self.client.get(reverse("ask:index")).status_code, 200)

    def test_terms_pages_are_exempt(self):
        self.assertEqual(self.client.get(reverse("ask:terms-view")).status_code, 200)
        self.assertEqual(self.client.get(reverse("ask:terms-accept")).status_code, 200)

    def test_admin_is_exempt(self):
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save()
        resp = self.client.get(reverse("admin:index"))
        self.assertEqual(resp.status_code, 200)

    def test_anonymous_user_not_sent_to_terms(self):
        self.client.logout()
        resp = self.client.get(reverse("ask:index"))
        self.assertTrue(resp.url.startswith(settings.LOGIN_URL))


class TermsViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", password="pw")
        self.client.force_login(self.user)

    def test_accept_page_renders_current_version(self):
        resp = self.client.get(reverse("ask:terms-accept"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["terms_version"], settings.TERMS_VERSION)
        self.assertFalse(TermsAcceptance.objects.exists())

    def test_post_records_acceptance(self):
        resp = self.client.post(reverse("ask:terms-accept"))
        self.assertRedirects(resp, reverse("ask:index"))
        self.assertTrue(TermsAcceptance.objects.filter(
            user=self.user, terms_version=settings.TERMS_VERSION
        ).exists())
        self.assertEqual(self.client.session["terms_accepted_version"], settings.TERMS_VERSION)

    def test_already_accepted_does_not_duplicate(self):
        _accept_terms(self.user)
        resp = self.client.post(reverse("ask:terms-accept"))
        self.assertRedirects(resp, reverse("ask:index"))
        self.assertEqual(TermsAcceptance.objects.filter(user=self.user).count(), 1)

    def test_terms_view_shows_acceptance(self):
        _accept_terms(self.user)
        resp = self.client.get(reverse("ask:terms-view"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["acceptance"].user, self.user)


def _http_status_error(status_code):
    request = httpx.Request("GET", "http://kb.test/")
    return httpx.HTTPStatusError(
        "error", request=request, response=httpx.Response(status_code, request=request)
    )


@patch("ask.tasks.close_old_connections")
class RunLlmTaskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", password="pw")
        self.conversation = Conversation.objects.create(user=self.user)
        self.record = QARecord.objects.create(
            conversation=self.conversation, user=self.user, question_text="q"
        )
        self.task = QueryTask.objects.create(user=self.user, query_text="q")

    def _run(self):
        run_llm_task(self.task.id, self.record.id, self.conversation.id)
        self.task.refresh_from_db()
        self.record.refresh_from_db()

    @patch("ask.llm_connector.query_llm")
    def test_success_stores_result(self, mock_query, _):
        content = json.dumps({"search_results": []})
        llm_response = {"success": True, "output": {"content": content}}
        mock_query.return_value = llm_response

        self._run()

        mock_query.assert_called_once_with(
            "q", llm_conversation_id=self.conversation.llm_conversation_id
        )
        self.assertEqual(self.task.status, QueryTask.Status.COMPLETED)
        self.assertEqual(self.task.result, content)
        self.assertEqual(self.record.answer_text, content)
        self.assertEqual(self.record.answer_raw_response, llm_response)
        self.assertIsNotNone(self.record.answer_timestamp)
        self.assertFalse(self.record.is_error)

    @patch("ask.llm_connector.query_llm")
    def test_malformed_response_marks_failed(self, mock_query, _):
        mock_query.return_value = {"success": False}
        with self.assertLogs("ask.tasks", level="ERROR"):
            self._run()
        self.assertEqual(self.task.status, QueryTask.Status.FAILED)
        self.assertEqual(self.task.error_message, "Something went wrong. Please try again.")
        self.assertTrue(self.record.is_error)
        self.assertIsNotNone(self.record.answer_timestamp)

    @patch("ask.llm_connector.query_llm", side_effect=httpx.ConnectError("down"))
    def test_connection_error_marks_failed(self, mock_query, _):
        with self.assertLogs("ask.tasks", level="ERROR"):
            self._run()
        self.assertEqual(self.task.status, QueryTask.Status.FAILED)
        self.assertTrue(self.record.is_error)


class EnrichSearchResultsTests(TestCase):
    def setUp(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media_root)
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_user("curator", password="pw")

    def _enrich(self, results):
        return json.loads(_enrich_search_results(json.dumps({"search_results": results})))["search_results"]

    def test_non_json_content_returned_unchanged(self):
        self.assertEqual(_enrich_search_results("plain text"), "plain text")
        self.assertEqual(_enrich_search_results(None), None)

    def test_missing_or_empty_results_returned_unchanged(self):
        content = json.dumps({"answer": "hi"})
        self.assertEqual(_enrich_search_results(content), content)
        content = json.dumps({"search_results": []})
        self.assertEqual(_enrich_search_results(content), content)

    def test_tracked_pdf_gets_local_url_and_publisher(self):
        pdf = PDFResource(
            title="Report", creator=self.user, mcp_kb_document_id=10, publisher="State"
        )
        pdf.file.save("report.pdf", ContentFile(b"%PDF-1.4"), save=True)
        [result] = self._enrich([{"document_id": "10-3", "url": "http://kb/doc"}])
        self.assertEqual(result["document_id"], 10)
        self.assertEqual(result["type"], "PDF")
        self.assertEqual(result["url"], pdf.file.url)
        self.assertEqual(result["publisher"], "State")

    def test_tracked_website_keeps_url_and_gets_publisher(self):
        WebsiteResource.objects.create(
            title="Site", url="https://example.com", creator=self.user,
            mcp_kb_document_id=20, publisher="Federal",
        )
        [result] = self._enrich([{"document_id": 20, "url": "https://example.com"}])
        self.assertEqual(result["type"], "Website")
        self.assertEqual(result["url"], "https://example.com")
        self.assertEqual(result["publisher"], "Federal")

    def test_untracked_results_infer_type_from_url(self):
        results = self._enrich([
            {"document_id": 99, "url": "https://example.com/file.PDF?x=1"},
            {"document_id": 98, "url": "https://example.com/page"},
            {"document_id": None, "url": ""},
        ])
        self.assertEqual([r["type"] for r in results], ["PDF", "Website", "PDF"])


class NormalizeDocIdTests(TestCase):
    def test_accepted_shapes(self):
        self.assertEqual(_normalize_doc_id(5), 5)
        self.assertEqual(_normalize_doc_id("12"), 12)
        self.assertEqual(_normalize_doc_id(" 12-3 "), 12)

    def test_rejected_shapes(self):
        for value in (True, False, None, "", "  ", "abc", "x-1", 1.5, [1]):
            with self.subTest(value=value):
                self.assertIsNone(_normalize_doc_id(value))


class QueryLlmTests(TestCase):
    def _mock_client(self, mock_client_cls):
        client = mock_client_cls.return_value.__enter__.return_value
        client.post.return_value.json.return_value = {"success": True}
        return client

    @override_settings(LLM_HOST="http://fallback.test/", LLM_TOKEN="secret", LLM_TIMEOUT=5)
    @patch("ask.llm_connector.httpx.Client")
    def test_falls_back_to_llm_host_without_active_workflow(self, mock_client_cls):
        client = self._mock_client(mock_client_cls)
        conversation_id = uuid.uuid4()

        self.assertEqual(query_llm("hello", llm_conversation_id=conversation_id), {"success": True})

        client.post.assert_called_once_with(
            "http://fallback.test/",
            json={"input": "hello", "conversationId": str(conversation_id)},
            headers={"X-API-Key": "secret", "Content-Type": "application/json"},
            timeout=5,
        )

    @override_settings(LLM_HOST="http://fallback.test/")
    @patch("ask.llm_connector.httpx.Client")
    def test_uses_active_workflow_endpoint(self, mock_client_cls):
        client = self._mock_client(mock_client_cls)
        SimWorkflow.objects.create(
            title="Agent", workflow_id="wf", is_active=True,
            agent_endpoint="http://agent.test/run",
        )
        query_llm("hello")
        self.assertEqual(client.post.call_args.args[0], "http://agent.test/run")

    @override_settings(LLM_HOST="http://fallback.test/")
    @patch("ask.llm_connector.httpx.Client")
    def test_active_workflow_without_endpoint_uses_llm_host(self, mock_client_cls):
        client = self._mock_client(mock_client_cls)
        SimWorkflow.objects.create(title="Agent", workflow_id="wf", is_active=True)
        query_llm("hello")
        self.assertEqual(client.post.call_args.args[0], "http://fallback.test/")

    @patch("ask.llm_connector.httpx.Client")
    def test_http_error_propagates(self, mock_client_cls):
        client = self._mock_client(mock_client_cls)
        client.post.return_value.raise_for_status.side_effect = _http_status_error(500)
        with self.assertRaises(httpx.HTTPStatusError):
            query_llm("hello")


class SimWorkflowTests(TestCase):
    def _workflow(self, name, is_active=False):
        return SimWorkflow.objects.create(title=name, workflow_id=name, is_active=is_active)

    def test_activating_deactivates_others_of_same_type(self):
        first = self._workflow("first", is_active=True)
        second = self._workflow("second", is_active=True)
        first.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertEqual(SimWorkflow.get_active(SimWorkflow.WorkflowType.AGENT), second)

    def test_cannot_deactivate_only_active_workflow(self):
        only = self._workflow("only", is_active=True)
        only.is_active = False
        with self.assertRaises(ValidationError):
            only.save()
        only.refresh_from_db()
        self.assertTrue(only.is_active)

    def test_inactive_workflow_can_be_created_and_edited(self):
        self._workflow("active", is_active=True)
        inactive = self._workflow("inactive")
        inactive.title = "renamed"
        inactive.save()
        self.assertEqual(SimWorkflow.objects.get(pk=inactive.pk).title, "renamed")

    def test_cannot_delete_only_active_workflow(self):
        only = self._workflow("only", is_active=True)
        with self.assertRaises(ValidationError):
            only.delete()
        self.assertTrue(SimWorkflow.objects.filter(pk=only.pk).exists())

    def test_inactive_workflow_can_be_deleted(self):
        self._workflow("active", is_active=True)
        inactive = self._workflow("inactive")
        inactive.delete()
        self.assertFalse(SimWorkflow.objects.filter(pk=inactive.pk).exists())

    def test_get_active_returns_none_when_nothing_active(self):
        self._workflow("inactive")
        self.assertIsNone(SimWorkflow.get_active(SimWorkflow.WorkflowType.AGENT))


class ContextProcessorTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user("alice", password="pw")

    def _request(self, user, session=None):
        request = self.factory.get("/")
        request.user = user
        request.session = session or {}
        return request

    @override_settings(SIDEBAR_CONVERSATIONS_LIMIT=2)
    def test_sidebar_lists_own_conversations_up_to_limit(self):
        for title in ("one", "two", "three"):
            Conversation.objects.create(user=self.user, title=title)
        Conversation.objects.create(
            user=User.objects.create_user("bob", password="pw"), title="bob's"
        )

        context = sidebar_conversations(self._request(self.user))

        self.assertEqual(context["sidebar_conversations_limit"], 2)
        labels = [item["label"] for item in context["sidebar_conversations"]]
        self.assertEqual(labels, ["three", "two"])
        item = context["sidebar_conversations"][0]
        self.assertEqual(
            item["url"], reverse("ask:conversation", kwargs={"conversation_id": item["id"]})
        )

    def test_untitled_conversation_labelled_by_date(self):
        conversation = Conversation.objects.create(user=self.user)
        [item] = sidebar_conversations(self._request(self.user))["sidebar_conversations"]
        self.assertEqual(item["label"], conversation.created_at.strftime("%b %d, %Y %I:%M %p"))

    def test_sidebar_empty_for_anonymous(self):
        context = sidebar_conversations(self._request(AnonymousUser()))
        self.assertEqual(context, {"sidebar_conversations": [], "sidebar_conversations_limit": 0})

    def test_terms_status(self):
        accepted = terms_status(self._request(
            self.user, {"terms_accepted_version": settings.TERMS_VERSION}
        ))
        self.assertTrue(accepted["terms_accepted"])
        self.assertFalse(terms_status(self._request(self.user))["terms_accepted"])
        self.assertEqual(terms_status(self._request(AnonymousUser())), {})


class KBResourcesViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("viewer", password="pw")
        _accept_terms(self.user)
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


class KBAddResourceViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("curator", password="pw")
        self.user.user_permissions.add(Permission.objects.get(codename="add_websiteresource"))
        _accept_terms(self.user)
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


class KBCompareViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("viewer", password="pw")
        _accept_terms(self.user)
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

    @patch("ask.views.list_kb_documents", side_effect=_http_status_error(500))
    def test_kb_http_error_returns_502(self, _):
        self.assertEqual(self._post().status_code, 502)

    @patch("ask.views.list_kb_documents", side_effect=RuntimeError("bug"))
    def test_unexpected_error_returns_500(self, _):
        with self.assertLogs("ask.views", level="ERROR"):
            self.assertEqual(self._post().status_code, 500)


class KBRemoveFromKBViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("curator", password="pw")
        self.user.user_permissions.add(Permission.objects.get(codename="delete_websiteresource"))
        _accept_terms(self.user)
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


class KBAddWebsiteToMcpViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("curator", password="pw")
        self.user.user_permissions.add(Permission.objects.get(codename="change_websiteresource"))
        _accept_terms(self.user)
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

    @patch("ask.views.add_website_to_kb", side_effect=_http_status_error(500))
    def test_kb_http_error_returns_502(self, _):
        self.assertEqual(self._post({"id": self.resource.id}).status_code, 502)
        self.resource.refresh_from_db()
        self.assertIsNone(self.resource.mcp_kb_document_id)

    def test_permission_required(self):
        self.user.user_permissions.clear()
        self.assertEqual(self._post({"id": self.resource.id}).status_code, 403)


class KBUploadPdfViewTests(TestCase):
    def setUp(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media_root)
        override.enable()
        self.addCleanup(override.disable)

        self.user = User.objects.create_user("curator", password="pw")
        self.user.user_permissions.add(Permission.objects.get(codename="add_pdfresource"))
        _accept_terms(self.user)
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
