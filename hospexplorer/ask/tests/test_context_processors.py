from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from ask.context_processors import sidebar_conversations, terms_status
from ask.models import Conversation


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
