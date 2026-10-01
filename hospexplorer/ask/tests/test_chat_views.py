import json
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from ask.models import Conversation, QARecord, QueryTask
from ask.tasks import run_llm_task
from ask.tests.utils import accept_terms


class ConversationViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", password="pw")
        accept_terms(self.user)
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
        accept_terms(self.user)
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
        accept_terms(self.user)
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
        accept_terms(self.user)
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


class MockResponseViewTests(TestCase):
    def test_returns_llm_shaped_payload(self):
        user = User.objects.create_user("alice", password="pw")
        accept_terms(user)
        self.client.force_login(user)
        body = self.client.get(reverse("ask:mock-response")).json()
        self.assertTrue(body["success"])
        self.assertTrue(body["output"]["content"])

    def test_requires_login(self):
        resp = self.client.get(reverse("ask:mock-response"))
        self.assertEqual(resp.status_code, 302)
