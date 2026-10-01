import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from ask.models import PDFResource, SimWorkflow


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
