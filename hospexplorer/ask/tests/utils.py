"""Shared helpers for the ask test suite."""

import shutil
import tempfile

import httpx
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings

from ask.models import TermsAcceptance


def accept_terms(user):
    TermsAcceptance.objects.create(user=user, terms_version=settings.TERMS_VERSION)


def http_status_error(status_code):
    request = httpx.Request("GET", "http://kb.test/")
    return httpx.HTTPStatusError(
        "error", request=request, response=httpx.Response(status_code, request=request)
    )


def message_texts(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


class AdminTestCase(TestCase):
    def setUp(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media_root)
        override.enable()
        self.addCleanup(override.disable)
        self.admin = User.objects.create_superuser("admin", "admin@example.com", "pw")
        self.client.force_login(self.admin)
