import io
import shutil
import tempfile
import zipfile
from unittest.mock import patch

import httpx
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from ask.models import (
    DocumentAuthorInstitution,
    DocumentType,
    InstitutionType,
    PDFResource,
    SimWorkflow,
    TermsAcceptance,
    WebsiteResource,
)
from ask.tasks import run_kb_resource_upload
from ask.tests.utils import AdminTestCase, message_texts


@patch("ask.admin.delete_kb_document")
class KBDeleteAdminTests(AdminTestCase):
    def _website(self, title, doc_id=None):
        return WebsiteResource.objects.create(
            title=title, url=f"https://{title}.test", creator=self.admin, mcp_kb_document_id=doc_id
        )

    def _delete(self, obj):
        return self.client.post(
            reverse("admin:ask_websiteresource_delete", args=[obj.pk]), {"post": "yes"}
        )

    def test_deletes_kb_document_then_row(self, mock_delete):
        site = self._website("site", doc_id=5)
        resp = self._delete(site)
        mock_delete.assert_called_once_with(5)
        self.assertFalse(WebsiteResource.objects.filter(pk=site.pk).exists())
        self.assertIn("Removed 'site' from Knowledge Base.", message_texts(resp))

    def test_row_without_kb_document_skips_kb_call(self, mock_delete):
        site = self._website("site")
        self._delete(site)
        mock_delete.assert_not_called()
        self.assertFalse(WebsiteResource.objects.filter(pk=site.pk).exists())

    def test_kb_failure_keeps_row(self, mock_delete):
        mock_delete.side_effect = httpx.ConnectError("down")
        site = self._website("site", doc_id=5)
        with self.assertLogs("ask.admin", level="ERROR"):
            resp = self._delete(site)
        self.assertTrue(WebsiteResource.objects.filter(pk=site.pk).exists())
        self.assertTrue(any("Kept 'site'" in m for m in message_texts(resp)))

    def test_bulk_delete_keeps_only_rows_whose_kb_delete_failed(self, mock_delete):
        def fail_for_doc_2(doc_id):
            if doc_id == 2:
                raise httpx.ConnectError("down")

        mock_delete.side_effect = fail_for_doc_2
        ok = self._website("ok", doc_id=1)
        failing = self._website("failing", doc_id=2)
        with self.assertLogs("ask.admin", level="ERROR"):
            self.client.post(reverse("admin:ask_websiteresource_changelist"), {
                "action": "delete_selected",
                "_selected_action": [ok.pk, failing.pk],
                "post": "yes",
            })
        self.assertFalse(WebsiteResource.objects.filter(pk=ok.pk).exists())
        self.assertTrue(WebsiteResource.objects.filter(pk=failing.pk).exists())

    def test_warns_when_pdf_file_cannot_be_removed(self, mock_delete):
        pdf = PDFResource(title="Sensitive", creator=self.admin)
        pdf.file.save("sensitive.pdf", ContentFile(b"%PDF-1.4"), save=True)
        with patch(
            "django.core.files.storage.FileSystemStorage.delete",
            side_effect=OSError("disk error"),
        ), self.assertLogs("ask.models", level="ERROR"):
            resp = self.client.post(
                reverse("admin:ask_pdfresource_delete", args=[pdf.pk]), {"post": "yes"}
            )
        self.assertFalse(PDFResource.objects.filter(pk=pdf.pk).exists())
        self.assertTrue(any("could not be removed" in m for m in message_texts(resp)))


@patch("ask.admin.threading.Thread")
class ResourceAdminSaveTests(AdminTestCase):
    def test_new_website_is_queued_for_kb_upload(self, mock_thread):
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(reverse("admin:ask_websiteresource_add"), {
                "title": "Site", "description": "", "url": "https://example.com",
                "date_published": "", "publisher": "",
            })
        self.assertEqual(resp.status_code, 302)
        site = WebsiteResource.objects.get(title="Site")
        self.assertEqual(site.creator, self.admin)
        self.assertEqual(site.modifier, self.admin)
        self.assertEqual(site.status, WebsiteResource.Status.PROCESSING)
        mock_thread.assert_called_once_with(
            target=run_kb_resource_upload, args=("website", site.pk), daemon=True
        )
        mock_thread.return_value.start.assert_called_once()

    def test_editing_website_keeps_creator(self, mock_thread):
        creator = User.objects.create_user("creator", password="pw")
        site = WebsiteResource.objects.create(
            title="Site", url="https://example.com", creator=creator
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("admin:ask_websiteresource_change", args=[site.pk]), {
                "title": "Renamed", "description": "", "url": "https://example.com",
                "date_published": "", "publisher": "",
            })
        site.refresh_from_db()
        self.assertEqual(site.title, "Renamed")
        self.assertEqual(site.creator, creator)
        self.assertEqual(site.modifier, self.admin)
        mock_thread.assert_called_once()

    def test_new_pdf_records_original_filename_and_is_queued(self, mock_thread):
        upload = SimpleUploadedFile("report.pdf", b"%PDF-1.4", content_type="application/pdf")
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(reverse("admin:ask_pdfresource_add"), {
                "title": "Report", "description": "", "file": upload,
                "date_published": "", "publisher": "",
            })
        self.assertEqual(resp.status_code, 302)
        pdf = PDFResource.objects.get(title="Report")
        self.assertEqual(pdf.original_filename, "report.pdf")
        self.assertEqual(pdf.status, PDFResource.Status.PROCESSING)
        mock_thread.assert_called_once_with(
            target=run_kb_resource_upload, args=("pdf", pdf.pk), daemon=True
        )


class SimWorkflowAdminTests(AdminTestCase):
    def _workflow(self, name, is_active=False):
        return SimWorkflow.objects.create(title=name, workflow_id=name, is_active=is_active)

    def _action(self, action, workflows):
        return self.client.post(reverse("admin:ask_simworkflow_changelist"), {
            "action": action,
            "_selected_action": [w.pk for w in workflows],
            "post": "yes",
        })

    def test_set_as_active_switches_active_workflow(self):
        old = self._workflow("old", is_active=True)
        new = self._workflow("new")
        resp = self._action("set_as_active", [new])
        self.assertIn("'new' is now the active workflow.", message_texts(resp))
        self.assertEqual(SimWorkflow.get_active(SimWorkflow.WorkflowType.AGENT), new)
        old.refresh_from_db()
        self.assertFalse(old.is_active)

    def test_set_as_active_requires_exactly_one_selection(self):
        active = self._workflow("active", is_active=True)
        a, b = self._workflow("a"), self._workflow("b")
        resp = self._action("set_as_active", [a, b])
        self.assertIn("Please select exactly one workflow to activate.", message_texts(resp))
        self.assertEqual(SimWorkflow.get_active(SimWorkflow.WorkflowType.AGENT), active)

    def test_deactivating_only_active_workflow_shows_error(self):
        only = self._workflow("only", is_active=True)
        resp = self.client.post(reverse("admin:ask_simworkflow_change", args=[only.pk]), {
            "title": "only", "description": "", "workflow_id": "only",
            "workflow_type": "agent", "agent_endpoint": "",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(any("Cannot deactivate" in m for m in message_texts(resp)))
        only.refresh_from_db()
        self.assertTrue(only.is_active)

    def test_deleting_only_active_workflow_shows_error(self):
        only = self._workflow("only", is_active=True)
        resp = self.client.post(
            reverse("admin:ask_simworkflow_delete", args=[only.pk]), {"post": "yes"}
        )
        self.assertTrue(any("Cannot delete" in m for m in message_texts(resp)))
        self.assertTrue(SimWorkflow.objects.filter(pk=only.pk).exists())

    def test_bulk_delete_of_only_active_workflow_shows_error(self):
        only = self._workflow("only", is_active=True)
        resp = self._action("delete_selected", [only])
        self.assertTrue(any("Cannot delete" in m for m in message_texts(resp)))
        self.assertTrue(SimWorkflow.objects.filter(pk=only.pk).exists())

    def test_bulk_delete_of_inactive_workflows(self):
        active = self._workflow("active", is_active=True)
        a, b = self._workflow("a"), self._workflow("b")
        self._action("delete_selected", [a, b])
        self.assertEqual(list(SimWorkflow.objects.all()), [active])


class ReadOnlyAndUserAdminTests(AdminTestCase):
    def test_terms_acceptances_cannot_be_added_or_deleted(self):
        acceptance = TermsAcceptance.objects.create(user=self.admin, terms_version="0.1")
        self.assertEqual(self.client.get(reverse("admin:ask_termsacceptance_add")).status_code, 403)
        resp = self.client.post(
            reverse("admin:ask_termsacceptance_delete", args=[acceptance.pk]), {"post": "yes"}
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(TermsAcceptance.objects.filter(pk=acceptance.pk).exists())

    def _add_user(self, email):
        return self.client.post(reverse("admin:auth_user_add"), {
            "username": "newbie", "email": email,
            "password1": "a-Strong-pass-123", "password2": "a-Strong-pass-123",
            "usable_password": "true",
        })

    def test_new_user_requires_email(self):
        resp = self._add_user("")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("email", resp.context["adminform"].form.errors)
        self.assertFalse(User.objects.filter(username="newbie").exists())

    def test_new_user_with_email_is_created(self):
        resp = self._add_user("newbie@example.com")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(User.objects.get(username="newbie").email, "newbie@example.com")


class LookupCSVImportAdminTests(AdminTestCase):
    URL_NAME = "admin:ask_documenttype_import_csv"

    def _post(self, data):
        return self.client.post(reverse(self.URL_NAME), data)

    def test_get_renders_upload_form(self):
        resp = self.client.get(reverse(self.URL_NAME))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["changelist_url"], reverse("admin:ask_documenttype_changelist"))

    def test_import_creates_rows(self):
        upload = SimpleUploadedFile("types.csv", b"name\nReport\nBrief\n", content_type="text/csv")
        resp = self._post({"csv_file": upload})
        self.assertRedirects(resp, reverse("admin:ask_documenttype_changelist"))
        self.assertIn(
            "Imported 2 new document types (skipped 1 duplicate or empty rows).", message_texts(resp)
        )
        self.assertEqual(DocumentType.objects.count(), 2)

    def test_import_works_for_each_lookup_model(self):
        for model, url_name in (
            (DocumentAuthorInstitution, "admin:ask_documentauthorinstitution_import_csv"),
            (InstitutionType, "admin:ask_institutiontype_import_csv"),
        ):
            with self.subTest(model=model.__name__):
                upload = SimpleUploadedFile("names.csv", b"WHO\n", content_type="text/csv")
                self.client.post(reverse(url_name), {"csv_file": upload})
                self.assertTrue(model.objects.filter(name="WHO").exists())

    def test_missing_file_rejected(self):
        resp = self._post({})
        self.assertIn("No file provided.", message_texts(resp))

    def test_non_csv_rejected(self):
        upload = SimpleUploadedFile("types.txt", b"Report\n")
        resp = self._post({"csv_file": upload})
        self.assertIn("File must have a .csv extension.", message_texts(resp))
        self.assertFalse(DocumentType.objects.exists())

    @patch("ask.admin.import_names_csv", side_effect=ValueError("bad data"))
    def test_import_error_reported(self, _):
        upload = SimpleUploadedFile("types.csv", b"Report\n")
        with self.assertLogs("ask.admin", level="ERROR"):
            resp = self._post({"csv_file": upload})
        self.assertIn("Import failed: bad data", message_texts(resp))


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


@override_settings(PDF_ZIP_CSV_COLUMNS=("filename", "title"))
class ZipUploadValidationTests(AdminTestCase):
    URL_NAME = "admin:ask_pdfresource_upload_zip"

    def _zip(self, members):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        buf.seek(0)
        buf.name = "upload.zip"
        return buf

    def _post(self, zip_file):
        return self.client.post(reverse(self.URL_NAME), {"zip_file": zip_file})

    def test_get_renders_upload_form(self):
        resp = self.client.get(reverse(self.URL_NAME))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["required_columns"], ("filename", "title"))

    def test_missing_file_rejected(self):
        resp = self.client.post(reverse(self.URL_NAME), {})
        self.assertIn("Please select a zip file to upload.", message_texts(resp))

    def test_invalid_zip_rejected(self):
        bad = SimpleUploadedFile("upload.zip", b"not a zip")
        resp = self._post(bad)
        self.assertIn("The uploaded file is not a valid zip archive.", message_texts(resp))

    def test_zip_without_csv_rejected(self):
        resp = self._post(self._zip({"report.pdf": b"%PDF"}))
        self.assertIn("Zip must contain one CSV metadata file (filename, title).", message_texts(resp))

    def test_zip_with_two_csvs_rejected(self):
        resp = self._post(self._zip({"a.csv": "filename,title\n", "b.csv": "filename,title\n"}))
        self.assertIn("Zip must contain exactly one CSV; found 2.", message_texts(resp))

    def test_csv_missing_required_column_rejected(self):
        resp = self._post(self._zip({"metadata.csv": "filename\nreport.pdf\n"}))
        self.assertIn("CSV is missing required columns: title.", message_texts(resp))
        self.assertFalse(PDFResource.objects.exists())

    def test_bad_rows_are_skipped_and_good_rows_imported(self):
        PDFResource.objects.create(
            title="Old Report", original_filename="old.pdf", creator=self.admin
        )
        csv_text = (
            "filename,title\r\n"
            "docs/nested.pdf,Nested Report\r\n"
            "untitled.pdf,\r\n"
            "missing.pdf,Missing Report\r\n"
            "old.pdf,Old Report\r\n"
        )
        zip_file = self._zip({
            "metadata.csv": csv_text,
            "folder/docs/nested.pdf": b"%PDF nested",
            "untitled.pdf": b"%PDF",
            "old.pdf": b"%PDF old",
            # macOS Finder metadata must not count as a second CSV
            "__MACOSX/._metadata.csv": b"junk",
        })

        resp = self._post(zip_file)

        self.assertRedirects(
            resp, reverse("admin:ask_pdfresource_changelist"), fetch_redirect_response=False
        )
        msgs = message_texts(resp)
        self.assertIn("Row 2: missing filename or title; skipped.", msgs)
        self.assertIn("Row 3: 'missing.pdf' not in zip; skipped.", msgs)
        self.assertIn("Row 4: 'old.pdf' already exists; skipped.", msgs)
        self.assertTrue(any(m.startswith("Imported 1 of 4 PDFs.") for m in msgs))
        nested = PDFResource.objects.get(title="Nested Report")
        self.assertEqual(nested.original_filename, "nested.pdf")
        self.assertEqual(nested.file.read(), b"%PDF nested")
        self.assertEqual(PDFResource.objects.count(), 2)

    @patch("ask.admin.threading.Thread")
    def test_kb_upload_started_for_each_imported_pdf(self, mock_thread):
        zip_file = self._zip({
            "metadata.csv": "filename,title\r\na.pdf,A\r\nb.pdf,B\r\n",
            "a.pdf": b"%PDF a",
            "b.pdf": b"%PDF b",
        })
        with self.captureOnCommitCallbacks(execute=True):
            self._post(zip_file)
        started = [c.kwargs["args"] for c in mock_thread.call_args_list]
        expected = [("pdf", pk) for pk in PDFResource.objects.order_by("pk").values_list("pk", flat=True)]
        self.assertEqual(sorted(started), sorted(expected))
        self.assertEqual(mock_thread.return_value.start.call_count, 2)

    @override_settings(PDF_ZIP_CSV_COLUMNS=("filename",))
    def test_misconfigured_columns_raise(self):
        with self.assertRaises(ImproperlyConfigured):
            self.client.get(reverse(self.URL_NAME))
