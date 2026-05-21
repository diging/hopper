import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from ask.models import PDFResource


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
