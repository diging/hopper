from django.urls import re_path
from ask import views

app_name = "ask"

urlpatterns = [
    re_path(r"^$", views.index, name="index"),
    re_path(r"^new/$", views.new_conversation, name="new-conversation"),
    re_path(r"^c/(?P<conversation_id>\d+)/$", views.conversation_detail, name="conversation"),
    re_path(r"^query/$", views.query, name="query-llm"),
    re_path(r"^poll/(?P<task_id>[0-9a-f-]+)/$", views.poll_query, name="poll-query"),
    re_path(r"^mock$", views.mock_response, name="mock-response"),
    re_path(r"^terms/$", views.terms_view, name="terms-view"),
    re_path(r"^terms/accept/$", views.terms_accept, name="terms-accept"),
    re_path(r"^history/delete$", views.delete_history, name="delete-history"),
    re_path(r"^kb/$", views.kb_resources, name="kb-resources"),
    re_path(r"^kb/compare/$", views.kb_compare, name="kb-compare"),
    re_path(r"^kb/add-resource/$", views.kb_add_resource, name="kb-add-resource"),
    re_path(r"^kb/add-pdf-resource/$", views.kb_add_pdf_resource, name="kb-add-pdf-resource"),
    re_path(r"^kb/remove-from-kb/$", views.kb_remove_from_kb, name="kb-remove-from-kb"),
    re_path(r"^kb/add-to-kb/$", views.kb_add_website_to_mcp, name="kb-add-to-kb"),
    re_path(r"^kb/upload-pdf/$", views.kb_upload_pdf, name="kb-upload-pdf"),
    re_path(r"^kb/add-pdf-to-kb/$", views.kb_add_pdf_to_mcp, name="kb-add-pdf-to-kb"),
]
