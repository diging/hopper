from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ask.models import TermsAcceptance
from ask.tests.utils import accept_terms


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
        accept_terms(self.user)
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
        accept_terms(self.user)
        resp = self.client.post(reverse("ask:terms-accept"))
        self.assertRedirects(resp, reverse("ask:index"))
        self.assertEqual(TermsAcceptance.objects.filter(user=self.user).count(), 1)

    def test_terms_view_shows_acceptance(self):
        accept_terms(self.user)
        resp = self.client.get(reverse("ask:terms-view"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["acceptance"].user, self.user)
