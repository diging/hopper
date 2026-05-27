import logging
import time

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


def add_website_to_kb(url):
    """Send a website URL to the MCP KB server for ingestion.

    Calls POST /docs/website/add?url={url} on the MCP KB server.
    The KB server fetches the page, chunks it, generates embeddings,
    and stores it for semantic search.
    """
    headers = {
        "Authorization": f"Bearer {settings.KB_MCP_JWT_TOKEN}",
    }
    endpoint = f"{settings.KB_MCP_HOST}/docs/website/add"

    with httpx.Client() as client:
        response = client.post(
            endpoint,
            params={"url": url},
            headers=headers,
            timeout=settings.KB_MCP_TIMEOUT,
        )

    response.raise_for_status()
    return response.json()


def add_pdf_to_kb(file_bytes, filename, title, url=None):
    """Upload a PDF to the MCP KB server for ingestion.

    Calls POST /docs/pdf/add on the MCP KB server with multipart form data.
    The KB server extracts text, chunks it, generates embeddings,
    and stores it for semantic search.
    """
    headers = {
        "Authorization": f"Bearer {settings.KB_MCP_JWT_TOKEN}",
    }
    endpoint = f"{settings.KB_MCP_HOST}/docs/pdf/add"

    data = {"title": title}
    if url:
        data["url"] = url

    # Only retry on transport errors (the request never completed) — a timeout
    # likely means the KB received the file and is still processing it, so
    # retrying would create a duplicate rather than help.
    attempts = max(1, settings.KB_MCP_PDF_RETRIES)
    last_exc = None
    for attempt in range(1, attempts + 1):
        # rebuild files each attempt: httpx consumes the stream on send
        files = {"file": (filename, file_bytes, "application/pdf")}
        try:
            with httpx.Client() as client:
                response = client.post(
                    endpoint,
                    headers=headers,
                    files=files,
                    data=data,
                    timeout=settings.KB_MCP_PDF_TIMEOUT,
                )
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException as e:
            # ReadTimeout/WriteTimeout are TransportError subclasses
            # let them propagate so the caller's timeout handler runs instead
            # of the retry loop
            logger.warning(
                "KB PDF push timed out for %s: %s; not retrying (KB may still be processing)",
                filename, e,
            )
            raise
        except httpx.TransportError as e:
            last_exc = e
            if attempt == attempts:
                break
            backoff = 2 ** (attempt - 1)
            logger.warning(
                "KB PDF push failed (attempt %d/%d) for %s: %s; retrying in %ds",
                attempt, attempts, filename, e, backoff,
            )
            time.sleep(backoff)

    raise last_exc


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
