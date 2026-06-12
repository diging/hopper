import json
import logging
import threading

from django.conf import settings
from django.shortcuts import render, redirect, get_object_or_404
from django.http import FileResponse, JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET, require_POST, require_http_methods

import httpx
from django.core.paginator import Paginator

from ask.models import Conversation, QARecord, QueryTask, TermsAcceptance, WebsiteResource, PDFResource
from ask.tasks import run_llm_task
from django.core.files.base import ContentFile

from ask.kb_connector import list_kb_documents, add_website_to_kb, add_pdf_to_kb, update_pdf_in_kb, delete_kb_document, download_kb_pdf

logger = logging.getLogger(__name__)


@login_required
def index(request):
    return render(request, "index.html")


@login_required
@require_POST
def new_conversation(request):
    """Create a new blank conversation and redirect to it."""
    conversation = Conversation.objects.create(user=request.user)
    return redirect("ask:conversation", conversation_id=conversation.id)


@login_required
def conversation_detail(request, conversation_id):
    """Display an existing conversation with all its QA records."""
    try:
        conversation = Conversation.objects.get(id=conversation_id, user=request.user)
    except Conversation.DoesNotExist:
        return redirect("ask:index")
    return render(request, "index.html", {
        "conversation": conversation,
    })


@login_required
def mock_response(request):
    """Returns a mock LLM response in the same format as the real server."""
    return JsonResponse({
        "success": True,
        "output": {
            "content": "Under the shimmering moonlit sky, a silver-maned unicorn named Luna trotted through the enchanted forest, her hooves leaving trails of stardust. When she discovered a wounded fox whimpering beneath an ancient oak, she touched her glowing horn to its paw, weaving magic that healed the hurt. With the fox curled beside her, Luna rested on a bed of moss, her heart full as the forest whispered lullabies, ensuring all creatures drifted into dreams of peace."
        }
    })


@login_required
@require_POST
def query(request):
    """
    Accept a user query via POST, create a background task, return task_id.
    The LLM call runs in a background thread to avoid HTTP timeouts.
    """
    try:
        body = json.loads(request.body)
        query_text = body.get("query", "").strip()
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({"error": "Invalid request body."}, status=400)

    if not query_text:
        return JsonResponse({"error": "No query provided."}, status=400)

    conversation_id = body.get("conversation_id")
    if conversation_id:
        conversation = get_object_or_404(
            Conversation, id=conversation_id, user=request.user
        )
    else:
        conversation = Conversation.objects.filter(user=request.user).first()
        if not conversation:
            conversation = Conversation.objects.create(user=request.user)

    record = QARecord.objects.create(
        conversation=conversation,
        question_text=query_text,
        user=request.user,
    )

    # conversation title is set from the first question
    if not conversation.title:
        conversation.title = query_text[:200]
        conversation.save()

    task = QueryTask.objects.create(
        user=request.user,
        query_text=query_text,
        status=QueryTask.Status.PENDING,
    )

    thread = threading.Thread(
        target=run_llm_task,
        args=(task.id, record.id, conversation.id),
        daemon=True,
    )
    thread.start()

    return JsonResponse({
        "task_id": str(task.id),
        "conversation_id": conversation.id,
        "conversation_title": conversation.title,
    })


@login_required
@require_GET
def poll_query(request, task_id):
    """Return the current status of a QueryTask. Only the owning user can poll."""
    try:
        task = QueryTask.objects.get(pk=task_id, user=request.user)
    except QueryTask.DoesNotExist:
        return JsonResponse({"error": "Task not found."}, status=404)

    response_data = {"status": task.status}

    if task.status == QueryTask.Status.COMPLETED:
        response_data["message"] = task.result
    elif task.status == QueryTask.Status.FAILED:
        response_data["error"] = task.error_message

    return JsonResponse(response_data)



@login_required
def terms_accept(request):
    current_version = settings.TERMS_VERSION
    already_accepted = TermsAcceptance.objects.filter(
        user=request.user,
        terms_version=current_version,
    ).exists()

    if already_accepted:
        request.session["terms_accepted_version"] = current_version
        return redirect("ask:index")

    if request.method == "POST":
        TermsAcceptance.objects.create(
            user=request.user,
            terms_version=current_version,
        )
        request.session["terms_accepted_version"] = current_version
        return redirect("ask:index")

    return render(request, "terms/terms_accept.html", {
        "terms_version": current_version,
    })


@login_required
def terms_view(request):
    current_version = settings.TERMS_VERSION
    acceptance = TermsAcceptance.objects.filter(
        user=request.user,
        terms_version=current_version,
    ).first()

    # Terms are stored in templates/terms/terms_of_use_content.html and is easily editable.
    # TERMS_VERSION in settings.py is used to track the version of the terms. The middleware checks each user's accepted
    # version (cached in session) against TERMS_VERSION anytime there is a mismatch, it redirects
    # them to terms_accept, where a new TermsAcceptance record is created with their IP and timestamp before they can access the app again.

    return render(request, "terms/terms_view.html", {
        "terms_version": current_version,
        "acceptance": acceptance,
    })


@login_required
@require_http_methods(["DELETE"])
def delete_history(request):
    request.user.conversations.all().delete()
    return JsonResponse({"message": "All conversations deleted successfully!"})


@login_required
def kb_resources(request):
    """Display paginated list of Knowledge Base resources from internal DB """

    websites = WebsiteResource.objects.all().order_by("-modified_at")
    web_paginator = Paginator(websites, settings.KB_RESOURCES_PAGE_SIZE)
    web_page_obj = web_paginator.get_page(request.GET.get("web_page", 1))

    pdfs = PDFResource.objects.all().order_by("-modified_at")
    pdf_paginator = Paginator(pdfs, settings.KB_RESOURCES_PAGE_SIZE)
    pdf_page_obj = pdf_paginator.get_page(request.GET.get("pdf_page", 1))

    # Curator permissions (ask.add/change/delete/view_websiteresource) are Django's default model permissions
    # either assign them to users via Django admin or by adding users to a
    # group that has these permissions (example: "curator" group)
    return render(request, "kb/resources.html", {
        "web_page_obj": web_page_obj,
        "pdf_page_obj": pdf_page_obj,
        "can_add": request.user.has_perm("ask.add_websiteresource"),
        "can_change": request.user.has_perm("ask.change_websiteresource"),
        "can_delete": request.user.has_perm("ask.delete_websiteresource"),
        "can_add_pdf": request.user.has_perm("ask.add_pdfresource"),
        "can_change_pdf": request.user.has_perm("ask.change_pdfresource"),
        "can_delete_pdf": request.user.has_perm("ask.delete_pdfresource"),
        "max_pdf_size_mb": settings.KB_PDF_MAX_SIZE_MB,
    })


@login_required
@require_POST
def kb_compare(request):
    """Compare internal WebsiteResource records with MCP KB documents.

    How it works:
    1. Paginates through ALL documents from the MCP KB server via GET /docs/list
       (each doc has: id, title, url, chunks). Collects all KB doc URLs into a set.
    2. Iterates over all internal WebsiteResource records from Django's DB.
       Compares each resource's URL against the KB URL set:
       - "in_kb": resource URL exists in KB — the resource is indexed
       - "missing_from_kb": resource URL NOT in KB — needs to be added/re-ingested
    3. Also finds "untracked" docs: URLs present in the KB but not in the
       internal WebsiteResource table (added to KB outside of this app).

    Returns JSON to the frontend:
    - resources: list of {id, url, title, status} for each internal resource
    - untracked: list of {url, title} for KB docs not tracked internally
    - kb_total: total documents in the KB
    - internal_total: total WebsiteResource records in Django DB
    """
    try:
        kb_docs = []
        page = 1
        while True:
            data = list_kb_documents(page=page, page_size=50)
            kb_docs.extend(data.get("documents", []))
            if len(kb_docs) >= data.get("total", 0):
                break
            page += 1

        # partition KB docs by type
        kb_websites = [d for d in kb_docs if d.get("doc_type") != "pdf"]
        kb_pdfs = [d for d in kb_docs if d.get("doc_type") == "pdf"]

        # website comparison (by URL)
        kb_urls = {doc["url"] for doc in kb_websites if doc.get("url")}
        internal_resources = WebsiteResource.objects.all()
        results = []
        internal_urls = set()
        for resource in internal_resources:
            internal_urls.add(resource.url)
            results.append({
                "id": resource.id,
                "url": resource.url,
                "title": resource.title,
                "status": "in_kb" if resource.url in kb_urls else "missing_from_kb",
            })

        untracked = [
            {"url": doc["url"], "title": doc["title"], "doc_id": doc["id"]}
            for doc in kb_websites
            if doc.get("url") and doc["url"] not in internal_urls
        ]

        # PDF comparison (by mcp_kb_document_id)
        kb_pdf_ids = {doc["id"] for doc in kb_pdfs}
        internal_pdfs = PDFResource.objects.all()
        pdf_results = []
        tracked_pdf_ids = set()
        for resource in internal_pdfs:
            if resource.mcp_kb_document_id is not None:
                tracked_pdf_ids.add(resource.mcp_kb_document_id)
            pdf_results.append({
                "id": resource.id,
                "title": resource.title,
                "filename": resource.file.name.split("/")[-1] if resource.file else "",
                "status": "in_kb" if resource.mcp_kb_document_id in kb_pdf_ids else "missing_from_kb",
            })

        untracked_pdfs = [
            {"title": doc["title"], "doc_id": doc["id"]}
            for doc in kb_pdfs
            if doc["id"] not in tracked_pdf_ids
        ]

        return JsonResponse({
            "success": True,
            "resources": results,
            "untracked": untracked,
            "pdf_results": pdf_results,
            "untracked_pdfs": untracked_pdfs,
            "kb_total": len(kb_docs),
            "internal_total": len(results) + len(pdf_results),
        })
    except httpx.ConnectError:
        return JsonResponse({
            "success": False,
            "error": "Could not connect to the Knowledge Base server.",
        }, status=503)
    except httpx.HTTPStatusError as e:
        return JsonResponse({
            "success": False,
            "error": f"Knowledge Base server returned an error (HTTP {e.response.status_code}).",
        }, status=502)
    except Exception:
        logger.exception("KB sync failed")
        return JsonResponse({
            "success": False,
            "error": "An unexpected error occurred during sync.",
        }, status=500)


@login_required
@require_POST
def kb_add_resource(request):
    """Create a WebsiteResource record for an untracked KB document.

    This tracks a KB document in Hopper's internal database without
    re-ingesting it — the document already exists in the KB.
    """
    
    # check if the user has the required permissions (default model permissions - see kb_resources view)
    if not request.user.has_perm("ask.add_websiteresource"):
        return JsonResponse({"success": False, "error": "Permission denied."}, status=403)

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid request body."}, status=400)

    url = body.get("url", "").strip()
    title = body.get("title", "").strip()
    doc_id = body.get("doc_id")
    if not url:
        return JsonResponse({"success": False, "error": "URL is required."}, status=400)

    try:
        doc_id = int(doc_id) if doc_id is not None else None
    except (TypeError, ValueError):
        doc_id = None

    # merge, don't reject if a Hopper resource already exists for this URL
    # (e.g. created but never linked to the KB), link it to the KB doc instead
    # of creating a duplicate. .first() is safe against pre-existing duplicates
    existing = WebsiteResource.objects.filter(url=url).first()
    if existing is not None:
        update_fields = ["modifier", "modified_at"]
        existing.modifier = request.user
        if doc_id is not None:
            existing.mcp_kb_document_id = doc_id
            update_fields.append("mcp_kb_document_id")
        existing.save(update_fields=update_fields)
        return JsonResponse({"success": True, "id": existing.id, "merged": True})

    resource = WebsiteResource.objects.create(
        url=url,
        title=title or url,
        mcp_kb_document_id=doc_id,
        creator=request.user,
        modifier=request.user,
    )
    return JsonResponse({"success": True, "id": resource.id, "merged": False})


@login_required
@require_POST
def kb_add_pdf_resource(request):
    """Create a PDFResource record for an untracked KB PDF document.

    This tracks a KB PDF in Hopper's internal database without re-ingesting
    or downloading the file — the document already exists in the KB, which
    remains the source of truth for the bytes. The new PDFResource has no
    local file attached.
    """

    if not request.user.has_perm("ask.add_pdfresource"):
        return JsonResponse({"success": False, "error": "Permission denied."}, status=403)

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid request body."}, status=400)

    doc_id = body.get("doc_id")
    title = (body.get("title") or "").strip()

    try:
        doc_id = int(doc_id)
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "error": "doc_id is required."}, status=400)

    # Merge, don't reject. 1) Already linked to this KB doc -> idempotent no-op.
    already = PDFResource.objects.filter(mcp_kb_document_id=doc_id).first()
    if already is not None:
        return JsonResponse({
            "success": True,
            "id": already.id,
            "title": already.title,
            "filename": already.file.name if already.file else "",
            "merged": True,
        })

    # 2) An unlinked local row whose title matches the KB doc (e.g. a zip upload
    #    that never linked to the KB) -> link it in place, no re-download.
    if title:
        candidate = PDFResource.objects.filter(
            mcp_kb_document_id__isnull=True, title=title
        ).first()
        if candidate is not None:
            candidate.mcp_kb_document_id = doc_id
            candidate.modifier = request.user
            candidate.status = PDFResource.Status.SUCCESS
            candidate.save(update_fields=[
                "mcp_kb_document_id", "modifier", "status", "modified_at",
            ])
            return JsonResponse({
                "success": True,
                "id": candidate.id,
                "title": candidate.title,
                "filename": candidate.file.name if candidate.file else "",
                "merged": True,
            })

    # 3) No local row -> download from the KB and create a new tracked row.
    try:
        filename, content = download_kb_pdf(doc_id)
    except httpx.ConnectError:
        return JsonResponse({"success": False, "error": "Could not connect to the Knowledge Base server."}, status=503)
    except httpx.HTTPStatusError as e:
        return JsonResponse({"success": False, "error": f"Knowledge Base server returned an error (HTTP {e.response.status_code})."}, status=502)

    file_field = None
    original_filename = ""
    status_message = "Tracked from KB; file not stored locally."
    if content is not None:
        file_field = ContentFile(content, name=filename)
        original_filename = filename
        status_message = ""

    resource = PDFResource.objects.create(
        title=title or f"Untitled KB doc {doc_id}",
        file=file_field,
        original_filename=original_filename,
        mcp_kb_document_id=doc_id,
        creator=request.user,
        modifier=request.user,
        status=PDFResource.Status.SUCCESS,
        status_message=status_message,
    )
    return JsonResponse({
        "success": True,
        "id": resource.id,
        "title": resource.title,
        "filename": resource.file.name if resource.file else "",
        "merged": False,
    })


@login_required
@require_POST
def kb_remove_from_kb(request):
    """Delete a document from the MCP KB server."""

    # check if the user has the required permissions (default model permissions - see kb_resources view)
    if not request.user.has_perm("ask.delete_websiteresource"):
        return JsonResponse({"success": False, "error": "Permission denied."}, status=403)

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid request body."}, status=400)

    doc_id = body.get("doc_id")
    if not doc_id:
        return JsonResponse({"success": False, "error": "doc_id is required."}, status=400)

    try:
        delete_kb_document(doc_id)
        return JsonResponse({"success": True})
    except httpx.ConnectError:
        return JsonResponse({"success": False, "error": "Could not connect to the Knowledge Base server."}, status=503)
    except httpx.HTTPStatusError as e:
        return JsonResponse({"success": False, "error": f"KB server error (HTTP {e.response.status_code})."}, status=502)


@login_required
@require_POST
def kb_add_website_to_mcp(request):
    """Re-ingest a WebsiteResource into the MCP KB server."""
    
    # check if the user has the required permissions (default model permissions - see kb_resources view)
    if not request.user.has_perm("ask.change_websiteresource"):
        return JsonResponse({"success": False, "error": "Permission denied."}, status=403)

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid request body."}, status=400)

    resource_id = body.get("id")
    if not resource_id:
        return JsonResponse({"success": False, "error": "Resource id is required."}, status=400)

    try:
        resource = WebsiteResource.objects.get(pk=resource_id)
    except WebsiteResource.DoesNotExist:
        return JsonResponse({"success": False, "error": "Resource not found."}, status=404)

    try:
        result = add_website_to_kb(resource.url)
        resource.mcp_kb_document_id = result.get("doc_id")
        resource.modifier = request.user
        resource.save(update_fields=["mcp_kb_document_id", "modifier", "modified_at"])
        return JsonResponse({"success": True, "doc_id": resource.mcp_kb_document_id})
    except httpx.ConnectError:
        return JsonResponse({"success": False, "error": "Could not connect to the Knowledge Base server."}, status=503)
    except httpx.HTTPStatusError as e:
        return JsonResponse({"success": False, "error": f"KB server error (HTTP {e.response.status_code})."}, status=502)


@login_required
@require_POST
def kb_upload_pdf(request):
    """Upload a PDF, store it locally, and send to MCP KB."""

    # check if the user has the required permissions (default model permissions - see kb_resources view)
    if not request.user.has_perm("ask.add_pdfresource"):
        return JsonResponse({"success": False, "error": "Permission denied."}, status=403)

    uploaded_file = request.FILES.get("file")
    title = request.POST.get("title", "").strip()

    if not uploaded_file:
        return JsonResponse({"success": False, "error": "No file provided."}, status=400)
    if not title:
        return JsonResponse({"success": False, "error": "Title is required."}, status=400)
    if not uploaded_file.name.lower().endswith(".pdf"):
        return JsonResponse({"success": False, "error": "Only PDF files are accepted."}, status=400)
    if uploaded_file.size > settings.KB_PDF_MAX_SIZE_MB * 1024 * 1024:
        return JsonResponse({"success": False, "error": f"File exceeds {settings.KB_PDF_MAX_SIZE_MB}MB limit."}, status=400)

    # read file bytes for the KB server upload, then reset the file pointer
    # so Django's FileField can read the same data again when saving to disk
    file_bytes = uploaded_file.read()
    uploaded_file.seek(0)

    resource = PDFResource(
        title=title,
        file=uploaded_file,
        creator=request.user,
        modifier=request.user,
    )
    resource.save()

    try:
        result = add_pdf_to_kb(file_bytes, uploaded_file.name, title)
        resource.mcp_kb_document_id = result.get("doc_id")
        resource.save(update_fields=["mcp_kb_document_id"])
        return JsonResponse({
            "success": True,
            "id": resource.id,
            "doc_id": resource.mcp_kb_document_id,
        })
    except Exception as e:
        logger.exception("Failed to send PDF to KB: %s", uploaded_file.name)
        return JsonResponse({
            "success": False,
            "id": resource.id,
            "warning": f"PDF saved locally but failed to send to Knowledge Base: {e}",
        })


@login_required
@require_POST
def kb_add_pdf_to_mcp(request):
    """Re-ingest an existing PDFResource into the MCP KB server."""

    # check if the user has the required permissions (default model permissions - see kb_resources view)
    if not request.user.has_perm("ask.change_pdfresource"):
        return JsonResponse({"success": False, "error": "Permission denied."}, status=403)

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid request body."}, status=400)

    resource_id = body.get("id")
    if not resource_id:
        return JsonResponse({"success": False, "error": "Resource id is required."}, status=400)

    try:
        resource = PDFResource.objects.get(pk=resource_id)
    except PDFResource.DoesNotExist:
        return JsonResponse({"success": False, "error": "Resource not found."}, status=404)

    try:
        resource.file.open("rb")
        file_bytes = resource.file.read()
        resource.file.close()
        filename = resource.file.name.split("/")[-1]
        # Re-ingest of an already-linked resource updates the existing KB doc by
        # id (no duplicate); an unlinked resource is added as a new KB doc.
        if resource.mcp_kb_document_id:
            result = update_pdf_in_kb(
                resource.mcp_kb_document_id, file_bytes, filename, resource.title
            )
        else:
            result = add_pdf_to_kb(file_bytes, filename, resource.title)
        resource.mcp_kb_document_id = result.get("doc_id")
        resource.modifier = request.user
        resource.save(update_fields=["mcp_kb_document_id", "modifier", "modified_at"])
        return JsonResponse({"success": True, "doc_id": resource.mcp_kb_document_id})
    except httpx.ConnectError:
        return JsonResponse({"success": False, "error": "Could not connect to the Knowledge Base server."}, status=503)
    except httpx.HTTPStatusError as e:
        try:
            kb_error = e.response.json().get("error", "")
        except Exception:
            kb_error = ""
        error_msg = kb_error if kb_error else f"KB server error (HTTP {e.response.status_code})."
        return JsonResponse({"success": False, "error": error_msg}, status=502)

@login_required
def get_pdf(request, filename):
    """Serve PDF files through Django with permission checks."""
    absolute_path = '{}/kb_pdfs/{}'.format(settings.MEDIA_ROOT, filename)
    response = FileResponse(open(absolute_path, 'rb'), as_attachment=False)
    return response 