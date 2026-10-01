import json
from unittest.mock import patch

import httpx
from django.test import TestCase, override_settings

from ask.kb_connector import (
    add_pdf_to_kb,
    add_website_to_kb,
    delete_kb_document,
    download_kb_pdf,
    list_kb_documents,
    update_pdf_in_kb,
)

# captured before any test patches httpx.Client, so the fake can build real clients
RealClient = httpx.Client


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


@override_settings(
    KB_MCP_HOST="http://kb.test",
    KB_MCP_JWT_TOKEN="test-token",
    KB_MCP_TIMEOUT=30,
    KB_MCP_PDF_TIMEOUT=300,
    KB_MCP_PDF_RETRIES=3,
)
class KBConnectorTestCase(TestCase):
    def serve(self, *outcomes):
        """Answer KB calls in order with the given responses or exceptions.

        Returns the list the sent requests are recorded into.
        """
        sent = []
        queue = list(outcomes)

        def handler(request):
            sent.append(request)
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        patcher = patch(
            "ask.kb_connector.httpx.Client",
            lambda: RealClient(transport=httpx.MockTransport(handler)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return sent

    def assertAuthorized(self, request):
        self.assertEqual(request.headers["Authorization"], "Bearer test-token")


class ListKBDocumentsTests(KBConnectorTestCase):
    def test_requests_page_and_returns_json(self):
        payload = {"total": 1, "page": 2, "page_size": 50, "documents": [{"id": 1}]}
        sent = self.serve(httpx.Response(200, json=payload))

        self.assertEqual(list_kb_documents(page=2, page_size=50), payload)

        [request] = sent
        self.assertEqual(request.method, "GET")
        self.assertEqual(str(request.url), "http://kb.test/docs/list?page=2&page_size=50")
        self.assertAuthorized(request)

    def test_http_error_raises(self):
        self.serve(httpx.Response(500))
        with self.assertRaises(httpx.HTTPStatusError):
            list_kb_documents()


class AddWebsiteToKBTests(KBConnectorTestCase):
    def test_posts_url_and_metadata(self):
        sent = self.serve(httpx.Response(200, json={"doc_id": 4}))

        result = add_website_to_kb("https://example.com/page", metadata={"publisher": "State"})

        self.assertEqual(result, {"doc_id": 4})
        [request] = sent
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/docs/website/add")
        self.assertEqual(request.url.params["url"], "https://example.com/page")
        self.assertEqual(json.loads(request.content), {"metadata": {"publisher": "State"}})
        self.assertAuthorized(request)

    def test_without_metadata_sends_empty_body(self):
        sent = self.serve(httpx.Response(200, json={"doc_id": 4}))
        add_website_to_kb("https://example.com")
        self.assertEqual(json.loads(sent[0].content), {})

    def test_http_error_raises(self):
        self.serve(httpx.Response(422))
        with self.assertRaises(httpx.HTTPStatusError):
            add_website_to_kb("https://example.com")


@patch("ask.kb_connector.time.sleep")
class AddPdfToKBTests(KBConnectorTestCase):
    def test_uploads_file_with_form_fields(self, mock_sleep):
        sent = self.serve(httpx.Response(200, json={"doc_id": 9}))

        result = add_pdf_to_kb(
            b"%PDF-1.4 body", "report.pdf", "Annual Report",
            url="https://example.com/report.pdf", metadata={"date_published": "2024"},
        )

        self.assertEqual(result, {"doc_id": 9})
        [request] = sent
        self.assertEqual(request.url.path, "/docs/pdf/add")
        self.assertAuthorized(request)
        body = request.read().decode()
        self.assertIn('name="file"; filename="report.pdf"', body)
        self.assertIn("%PDF-1.4 body", body)
        self.assertIn('name="title"\r\n\r\nAnnual Report', body)
        self.assertIn('name="url"\r\n\r\nhttps://example.com/report.pdf', body)
        self.assertIn(json.dumps({"date_published": "2024"}), body)
        mock_sleep.assert_not_called()

    def test_optional_fields_omitted(self, mock_sleep):
        sent = self.serve(httpx.Response(200, json={"doc_id": 9}))
        add_pdf_to_kb(b"%PDF", "report.pdf", "Report")
        body = sent[0].read().decode()
        self.assertNotIn('name="url"', body)
        self.assertNotIn('name="metadata"', body)

    def test_retries_connection_errors_with_backoff(self, mock_sleep):
        sent = self.serve(
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused"),
            httpx.Response(200, json={"doc_id": 9}),
        )
        with self.assertLogs("ask.kb_connector", level="WARNING"):
            result = add_pdf_to_kb(b"%PDF", "report.pdf", "Report")
        self.assertEqual(result, {"doc_id": 9})
        self.assertEqual(len(sent), 3)
        self.assertEqual([c.args[0] for c in mock_sleep.call_args_list], [1, 2])

    def test_gives_up_after_configured_attempts(self, mock_sleep):
        sent = self.serve(*[httpx.ConnectError(f"refused {i}") for i in range(3)])
        with self.assertLogs("ask.kb_connector", level="WARNING"):
            with self.assertRaisesMessage(httpx.ConnectError, "refused 2"):
                add_pdf_to_kb(b"%PDF", "report.pdf", "Report")
        self.assertEqual(len(sent), 3)

    @override_settings(KB_MCP_PDF_RETRIES=0)
    def test_zero_retries_still_makes_one_attempt(self, mock_sleep):
        sent = self.serve(httpx.ConnectError("refused"))
        with self.assertRaises(httpx.ConnectError):
            add_pdf_to_kb(b"%PDF", "report.pdf", "Report")
        self.assertEqual(len(sent), 1)
        mock_sleep.assert_not_called()

    def test_timeout_is_not_retried(self, mock_sleep):
        # the KB probably received the file, so retrying would duplicate it
        sent = self.serve(httpx.ReadTimeout("slow"))
        with self.assertLogs("ask.kb_connector", level="WARNING"):
            with self.assertRaises(httpx.ReadTimeout):
                add_pdf_to_kb(b"%PDF", "report.pdf", "Report")
        self.assertEqual(len(sent), 1)
        mock_sleep.assert_not_called()

    def test_http_error_is_not_retried(self, mock_sleep):
        sent = self.serve(httpx.Response(500))
        with self.assertRaises(httpx.HTTPStatusError):
            add_pdf_to_kb(b"%PDF", "report.pdf", "Report")
        self.assertEqual(len(sent), 1)


@patch("ask.kb_connector.time.sleep")
class UpdatePdfInKBTests(KBConnectorTestCase):
    def test_uploads_file_with_doc_id(self, mock_sleep):
        sent = self.serve(httpx.Response(200, json={"doc_id": 12}))

        result = update_pdf_in_kb(12, b"%PDF new", "report.pdf", "Report", url="https://x.test/r.pdf")

        self.assertEqual(result, {"doc_id": 12})
        [request] = sent
        self.assertEqual(request.url.path, "/docs/pdf/update")
        self.assertAuthorized(request)
        body = request.read().decode()
        self.assertIn('name="doc_id"\r\n\r\n12', body)
        self.assertIn('name="title"\r\n\r\nReport', body)
        self.assertIn('name="url"\r\n\r\nhttps://x.test/r.pdf', body)
        self.assertIn("%PDF new", body)

    def test_retries_connection_errors(self, mock_sleep):
        sent = self.serve(httpx.ConnectError("refused"), httpx.Response(200, json={"doc_id": 12}))
        with self.assertLogs("ask.kb_connector", level="WARNING"):
            self.assertEqual(update_pdf_in_kb(12, b"%PDF", "r.pdf", "R"), {"doc_id": 12})
        self.assertEqual(len(sent), 2)
        mock_sleep.assert_called_once_with(1)

    def test_gives_up_after_configured_attempts(self, mock_sleep):
        sent = self.serve(*[httpx.ConnectError("refused") for _ in range(3)])
        with self.assertLogs("ask.kb_connector", level="WARNING"):
            with self.assertRaises(httpx.ConnectError):
                update_pdf_in_kb(12, b"%PDF", "r.pdf", "R")
        self.assertEqual(len(sent), 3)

    def test_timeout_is_not_retried(self, mock_sleep):
        sent = self.serve(httpx.ReadTimeout("slow"))
        with self.assertLogs("ask.kb_connector", level="WARNING"):
            with self.assertRaises(httpx.ReadTimeout):
                update_pdf_in_kb(12, b"%PDF", "r.pdf", "R")
        self.assertEqual(len(sent), 1)
        mock_sleep.assert_not_called()


class DownloadKBPdfRequestTests(KBConnectorTestCase):
    def test_requests_document_file(self):
        sent = self.serve(httpx.Response(
            200, content=b"%PDF", headers={"content-disposition": "attachment; filename=plain.pdf"}
        ))
        self.assertEqual(download_kb_pdf(5), ("plain.pdf", b"%PDF"))
        self.assertEqual(sent[0].url.path, "/docs/5/file")
        self.assertAuthorized(sent[0])

    def test_server_error_raises(self):
        self.serve(httpx.Response(500))
        with self.assertRaises(httpx.HTTPStatusError):
            download_kb_pdf(5)


class DeleteKBDocumentTests(KBConnectorTestCase):
    def test_sends_delete_and_returns_json(self):
        sent = self.serve(httpx.Response(200, json={"deleted": 5}))
        self.assertEqual(delete_kb_document(5), {"deleted": 5})
        [request] = sent
        self.assertEqual(request.method, "DELETE")
        self.assertEqual(request.url.path, "/docs/5")
        self.assertAuthorized(request)

    def test_http_error_raises(self):
        self.serve(httpx.Response(404))
        with self.assertRaises(httpx.HTTPStatusError):
            delete_kb_document(5)
