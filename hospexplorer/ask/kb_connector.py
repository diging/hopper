import json
import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


def list_kb_documents(page=1, page_size=10):
    """Call MCP KB docs/list endpoint. Returns parsed JSON response.

    Response format: {total, page, page_size, documents: [{id, title, url, chunks: [...]}]}
    """
    headers = {
        "Authorization": f"Bearer {settings.KB_MCP_JWT_TOKEN}",
        "Content-Type": "application/json",
    }
    url = f"{settings.KB_MCP_HOST}/docs/list"
    params = {"page": page, "page_size": page_size}

    with httpx.Client() as client:
        response = client.get(
            url,
            headers=headers,
            params=params,
            timeout=settings.KB_MCP_TIMEOUT,
        )

    response.raise_for_status()
    return response.json()


def add_website_to_kb(url, metadata=None):
    """Send a website URL to the MCP KB server for ingestion.

    Calls POST /docs/website/add?url={url} on the MCP KB server.
    ``metadata`` (if provided) is sent as a JSON body ``{"metadata": ...}`` so
    the KB server can store it on the Document row.
    """
    headers = {
        "Authorization": f"Bearer {settings.KB_MCP_JWT_TOKEN}",
        "Content-Type": "application/json",
    }
    endpoint = f"{settings.KB_MCP_HOST}/docs/website/add"

    with httpx.Client() as client:
        response = client.post(
            endpoint,
            params={"url": url},
            json={"metadata": metadata} if metadata is not None else {},
            headers=headers,
            timeout=settings.KB_MCP_TIMEOUT,
        )

    response.raise_for_status()
    return response.json()


def add_pdf_to_kb(file_bytes, filename, title, url=None, metadata=None):
    """Upload a PDF to the MCP KB server for ingestion.

    Calls POST /docs/pdf/add on the MCP KB server with multipart form data.
    metadata (if provided) is JSON-encoded into a metadata form field so
    it can travel alongside the file.
    """
    headers = {
        "Authorization": f"Bearer {settings.KB_MCP_JWT_TOKEN}",
    }
    endpoint = f"{settings.KB_MCP_HOST}/docs/pdf/add"

    files = {"file": (filename, file_bytes, "application/pdf")}
    data = {"title": title}
    if url:
        data["url"] = url
    if metadata is not None:
        data["metadata"] = json.dumps(metadata)

    with httpx.Client() as client:
        response = client.post(
            endpoint,
            headers=headers,
            files=files,
            data=data,
            timeout=settings.KB_MCP_TIMEOUT,
        )

    response.raise_for_status()
    return response.json()


def delete_kb_document(doc_id):
    """Delete a document from the MCP KB server by its ID.

    Calls DELETE /docs/{doc_id} on the MCP KB server.
    The KB server removes the document and all its chunks.
    """
    headers = {
        "Authorization": f"Bearer {settings.KB_MCP_JWT_TOKEN}",
    }
    endpoint = f"{settings.KB_MCP_HOST}/docs/{doc_id}"

    logger.debug("Deleting document from KB: doc_id=%s", doc_id)
    with httpx.Client() as client:
        response = client.delete(
            endpoint,
            headers=headers,
            timeout=settings.KB_MCP_TIMEOUT,
        )

    response.raise_for_status()
    logger.info("Deleted document from KB: doc_id=%s", doc_id)
    return response.json()
