import json
import shutil
import tempfile
import uuid
from unittest.mock import patch

import httpx
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from ask.llm_connector import query_llm
from ask.models import (
    Conversation,
    PDFResource,
    QARecord,
    QueryTask,
    SimWorkflow,
    WebsiteResource,
)
from ask.tasks import _enrich_search_results, _normalize_doc_id, run_llm_task
from ask.tests.utils import http_status_error


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
        client.post.return_value.raise_for_status.side_effect = http_status_error(500)
        with self.assertRaises(httpx.HTTPStatusError):
            query_llm("hello")
