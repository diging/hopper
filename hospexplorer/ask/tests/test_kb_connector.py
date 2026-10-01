from unittest.mock import patch

from django.test import TestCase


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
