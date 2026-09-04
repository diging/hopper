import csv
import io
import logging
import os
import threading
import zipfile

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import path, reverse

from ask.models import (
    Conversation,
    TermsAcceptance,
    QARecord,
    SimWorkflow,
    WebsiteResource,
    PDFResource,
    DocumentType,
    DocumentAuthorInstitution,
    InstitutionType,
)
from ask.admin_csv import import_names_csv, validate_partial_date
from ask.kb_connector import delete_kb_document
from ask.tasks import run_kb_resource_upload

logger = logging.getLogger(__name__)


class KBDeleteAdminMixin:
    """ModelAdmin mixin: deletes the KB counterpart before the local row; keeps the row on KB failure."""

    def _delete_kb_document(self, request, obj):
        if not obj.mcp_kb_document_id:
            return True
        try:
            delete_kb_document(obj.mcp_kb_document_id)
        except Exception as e:
            logger.exception(
                "Failed to delete %s from KB: doc_id=%s",
                obj._meta.verbose_name, obj.mcp_kb_document_id,
            )
            self.message_user(
                request,
                f"Kept '{obj.title}' — failed to remove from Knowledge Base: {e}",
                level="error",
            )
            return False
        self.message_user(request, f"Removed '{obj.title}' from Knowledge Base.")
        return True

    def _warn_if_file_remains(self, request, obj):
        # PDFResource.delete() sets this flag when its media file could not be
        # removed, the error below is reported to the user
        if getattr(obj, "file_deletion_failed", False):
            self.message_user(
                request,
                f"'{obj.title}' was deleted, but its file could not be removed from "
                "the server. The file may still be accessible and contain sensitive "
                "data. Please delete it manually.",
                level="error",
            )

    def delete_model(self, request, obj):
        if not self._delete_kb_document(request, obj):
            return
        super().delete_model(request, obj)
        self._warn_if_file_remains(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            if not self._delete_kb_document(request, obj):
                continue
            obj.delete()
            self._warn_if_file_remains(request, obj)


class QARecordInline(admin.TabularInline):
    model = QARecord
    extra = 0
    readonly_fields = ("question_text", "question_timestamp", "answer_text", "answer_timestamp", "is_error")
    fields = ("question_text", "question_timestamp", "answer_text", "answer_timestamp", "is_error")


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "llm_conversation_id", "title", "user", "qa_record_count", "created_at", "updated_at")
    list_filter = ("user",)
    search_fields = ("title", "user__username")
    readonly_fields = ("id", "llm_conversation_id", "qa_record_count", "created_at", "updated_at")

    def qa_record_count(self, obj):
        return obj.qa_records.count()
    qa_record_count.short_description = "Q&A Records"


class CustomUserAdmin(UserAdmin):
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'email', 'password1', 'password2'),
        }),
    )

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj is None:
            form.base_fields['email'].required = True
        return form


admin.site.unregister(User)
admin.site.register(User, CustomUserAdmin)


@admin.register(TermsAcceptance)
class TermsAcceptanceAdmin(admin.ModelAdmin):
    list_display = ("user", "terms_version", "accepted_at")
    list_filter = ("terms_version", "accepted_at")
    search_fields = ("user__username", "user__email")
    readonly_fields = ("user", "terms_version", "accepted_at")
    ordering = ("-accepted_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(QARecord)
class QARecordAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "conversation", "truncated_question", "question_timestamp", "answer_timestamp", "is_error"]
    list_filter = ["question_timestamp", "user", "is_error"]
    search_fields = ["question_text", "answer_text", "user__username"]
    readonly_fields = ["question_timestamp", "answer_timestamp", "answer_raw_response"]
    raw_id_fields = ["user", "conversation"]
    date_hierarchy = "question_timestamp"

    def truncated_question(self, obj):
        return obj.question_text[:75] + "..." if len(obj.question_text) > 75 else obj.question_text
    truncated_question.short_description = "Question"


@admin.register(SimWorkflow)
class SimWorkflowAdmin(admin.ModelAdmin):
    list_display = ("title", "workflow_id", "workflow_type", "is_active", "agent_endpoint", "updated_at")
    list_filter = ("is_active", "workflow_type")
    search_fields = ("title", "description", "workflow_id")
    actions = ["set_as_active"]
    fieldsets = (
        (None, {
            "fields": ("title", "description", "workflow_id", "workflow_type"),
        }),
        ("Endpoint", {
            "fields": ("agent_endpoint",),
            "description": "The active workflow's endpoint is used by the LLM connector. "
                           "If no workflow is active or the endpoint is blank, the LLM_HOST env variable is used as fallback.",
        }),
        ("Status", {
            "fields": ("is_active",),
            "description": "Only one workflow per type can be active. "
                           "Activating this workflow will deactivate others of the same type. "
                           "You cannot deactivate or delete the last active workflow of a type.",
        }),
    )

    # action to select exactly one workflow and activate it
    # the model's save() will auto-deactivate all others
    @admin.action(description="Set selected workflow as active")
    def set_as_active(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Please select exactly one workflow to activate.", level="error")
            return
        workflow = queryset.first()
        workflow.is_active = True
        workflow.save()
        self.message_user(request, f"'{workflow.title}' is now the active workflow.")

    # catches ValidationError from model constraints
    # and gives an admin message instead of a 500 error
    def save_model(self, request, obj, form, change):
        from django.core.exceptions import ValidationError
        try:
            obj.save()
        except ValidationError as e:
            self.message_user(request, e.message, level="error")

    # catches ValidationError when trying to delete the only active workflow.
    def delete_model(self, request, obj):
        from django.core.exceptions import ValidationError
        try:
            obj.delete()
        except ValidationError as e:
            self.message_user(request, e.message, level="error")

    # handles bulk delete; deletes one by one so the active workflow constraint is checked per object
    # stops if the only active workflow is being deleted
    def delete_queryset(self, request, queryset):
        from django.core.exceptions import ValidationError
        for obj in queryset:
            try:
                obj.delete()
            except ValidationError as e:
                self.message_user(request, e.message, level="error")
                return


class LookupCSVImportMixin:
    """Adds an Import CSV button + upload view to a lookup ModelAdmin.

    CSV is single-column name. Duplicates are skipped, header row optional.
    """

    change_list_template = "admin/ask/lookup_change_list.html"

    def get_urls(self):
        urls = super().get_urls()
        info = (self.model._meta.app_label, self.model._meta.model_name)
        return [
            path(
                "import-csv/",
                self.admin_site.admin_view(self.import_csv_view),
                name=f"{info[0]}_{info[1]}_import_csv",
            ),
        ] + urls

    def import_csv_view(self, request):
        info = (self.model._meta.app_label, self.model._meta.model_name)
        changelist_url = reverse(f"admin:{info[0]}_{info[1]}_changelist")

        if request.method == "POST":
            file_obj = request.FILES.get("csv_file")
            if file_obj is None:
                self.message_user(request, "No file provided.", level="error")
            elif not file_obj.name.lower().endswith(".csv"):
                self.message_user(request, "File must have a .csv extension.", level="error")
            else:
                try:
                    created, skipped = import_names_csv(self.model, file_obj)
                except Exception as e:
                    logger.exception("CSV import failed for %s", self.model.__name__)
                    self.message_user(request, f"Import failed: {e}", level="error")
                else:
                    self.message_user(
                        request,
                        f"Imported {created} new {self.model._meta.verbose_name_plural} "
                        f"(skipped {skipped} duplicate or empty rows).",
                    )
            return HttpResponseRedirect(changelist_url)

        context = {
            **self.admin_site.each_context(request),
            "title": f"Import {self.model._meta.verbose_name_plural} from CSV",
            "opts": self.model._meta,
            "changelist_url": changelist_url,
        }
        return render(request, "admin/ask/lookup_csv_import.html", context)


@admin.register(DocumentType)
class DocumentTypeAdmin(LookupCSVImportMixin, admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


@admin.register(DocumentAuthorInstitution)
class DocumentAuthorInstitutionAdmin(LookupCSVImportMixin, admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


@admin.register(InstitutionType)
class InstitutionTypeAdmin(LookupCSVImportMixin, admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


@admin.register(WebsiteResource)
class WebsiteResourceAdmin(KBDeleteAdminMixin, admin.ModelAdmin):
    list_display = ("title", "url", "creator", "status", "modified_at")
    list_filter = ("status",)
    search_fields = ("title", "url")
    readonly_fields = ("created_at", "modified_at", "creator", "modifier", "mcp_kb_document_id", "status", "status_message")
    fieldsets = (
        (None, {"fields": ("title", "description", "url")}),
        ("Metadata", {"fields": (
            "date_published",
            "document_type", "document_author_institution", "institution_type", "publisher"
        )}),
        ("Status", {"fields": (
            "status", "status_message", "mcp_kb_document_id",
            "created_at", "modified_at", "creator", "modifier",
        )}),
    )
    help_texts = {
        "title": "A short name to identify this website resource.",
        "description": "Optional details about what this website covers.",
        "url": "The URL the LLM will use as context when answering questions.",
        "date_published": "Partial ISO date: YYYY, YYYY-MM, or YYYY-MM-DD. Leave blank if unknown.",
        "publisher": "The publisher of this website resource.",
    }

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        for field_name, text in self.help_texts.items():
            if field_name in form.base_fields:
                form.base_fields[field_name].help_text = text
        return form

    def save_model(self, request, obj, form, change):
        if not change:
            obj.creator = request.user
        obj.modifier = request.user
        obj.status = WebsiteResource.Status.PROCESSING
        obj.status_message = "Queued for Knowledge Base upload."
        super().save_model(request, obj, form, change)

        # start MCP KB upload in a background thread AFTER the admin's
        # transaction commits, so a slow MCP round trip wont time out the save
        transaction.on_commit(
            lambda: threading.Thread(
                target=run_kb_resource_upload,
                args=("website", obj.pk),
                daemon=True,
            ).start()
        )
        self.message_user(
            request,
            f"Website '{obj.title}' saved. Upload to Knowledge Base is running in the background — "
            "refresh this page to see the final status.",
        )


# Optional metadata columns the zip-CSV importer reads onto each PDFResource.
# Controlled-list values create the matching lookup row the first time they
# appear, so the available options grow from what the imports actually use.
ZIP_CSV_LOOKUP_COLUMNS = {
    "document_type": DocumentType,
    "document_author_institution": DocumentAuthorInstitution,
    "institution_type": InstitutionType,
}


def _apply_zip_csv_metadata(obj, row):
    """Populate a resource's metadata fields from one zip-CSV row.

    Every metadata column is optional. Returns a list of human-readable
    warnings for values that could not be applied — the row is still imported,
    just with that field left blank.
    """
    warnings = []

    date_raw = (row.get("Date Published") or "").strip()
    if date_raw:
        try:
            obj.date_published = validate_partial_date(date_raw)
        except ValueError:
            warnings.append(
                f"invalid Date Published '{date_raw}' "
                "(use YYYY, YYYY-MM or YYYY-MM-DD); left blank"
            )

    institution_raw = (row.get("Document Author Institution") or "").strip()
    if institution_raw:
        try:
            obj.document_author_institution = DocumentAuthorInstitution.objects.get_or_create(name=institution_raw)[0]
        except DocumentAuthorInstitution.DoesNotExist:
            warnings.append(f"invalid Institution '{institution_raw}'; left blank")

    institution_type_raw = (row.get("Institution Type") or "").strip()
    if institution_type_raw:
        try:
            obj.institution_type = InstitutionType.objects.get_or_create(name=institution_type_raw)[0]
        except InstitutionType.DoesNotExist:
            warnings.append(f"invalid Institution Type '{institution_type_raw}'; left blank")

    document_type_raw = (row.get("Document Type") or "").strip()
    if document_type_raw:
        try:
            obj.document_type = DocumentType.objects.get_or_create(name=document_type_raw)[0]
        except DocumentType.DoesNotExist:
            warnings.append(f"invalid Document Type '{document_type_raw}'; left blank")

    publisher_raw = (row.get("Publisher") or "").strip()
    if publisher_raw:
        obj.publisher = publisher_raw

    for column, model in ZIP_CSV_LOOKUP_COLUMNS.items():
        value = (row.get(column) or "").strip()
        if not value:
            continue
        if len(value) > 255:
            warnings.append(f"{column} value exceeds 255 characters; left blank")
            continue
        lookup, _ = model.objects.get_or_create(name=value)
        setattr(obj, column, lookup)

    return warnings


@admin.register(PDFResource)
class PDFResourceAdmin(KBDeleteAdminMixin, admin.ModelAdmin):
    list_display = ("title", "file", "creator", "status", "modified_at")
    list_filter = ("status",)
    search_fields = ("title",)
    readonly_fields = ("created_at", "modified_at", "creator", "modifier", "mcp_kb_document_id", "status", "status_message")
    fieldsets = (
        (None, {"fields": ("title", "description", "file")}),
        ("Metadata", {"fields": (
            "date_published",
            "document_type", "document_author_institution", "institution_type", "publisher",
        )}),
        ("Status", {"fields": (
            "status", "status_message", "mcp_kb_document_id",
            "created_at", "modified_at", "creator", "modifier",
        )}),
    )
    help_texts = {
        "title": "A short name to identify this PDF resource.",
        "description": "Optional details about what this PDF covers.",
        "file": "The PDF file the LLM will use as context when answering questions.",
        "date_published": "Partial ISO date: YYYY, YYYY-MM, or YYYY-MM-DD. Leave blank if unknown.",
        "publisher": "The publisher of this PDF resource.",
    }

    # Column names the bulk-import CSV must define (first = zip member, second = resource title)
    # Defaults come from settings.PDF_ZIP_CSV_COLUMNS (override on a subclass if needed)
    @property
    def zip_csv_required_columns(self):
        cols = tuple(settings.PDF_ZIP_CSV_COLUMNS)
        if len(cols) < 2:
            raise ImproperlyConfigured(
                "PDF_ZIP_CSV_COLUMNS must list at least two column names "
                "(filename column first, title column second)."
            )
        return cols[:2]

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        for field_name, text in self.help_texts.items():
            if field_name in form.base_fields:
                form.base_fields[field_name].help_text = text
        return form

    def save_model(self, request, obj, form, change):
        if not change:
            obj.creator = request.user
        obj.modifier = request.user
        obj.status = PDFResource.Status.PROCESSING
        obj.status_message = "Queued for Knowledge Base upload."
        
        # record the original name so the zip upload's duplicate check sees PDFs
        # added through this form too; do it before save() mangles file.name on collision
        if not change or "file" in form.changed_data:
            obj.original_filename = os.path.basename(obj.file.name)
        
        super().save_model(request, obj, form, change)

        transaction.on_commit(
            lambda: threading.Thread(
                target=run_kb_resource_upload,
                args=("pdf", obj.pk),
                daemon=True,
            ).start()
        )
        self.message_user(
            request,
            f"PDF '{obj.title}' saved. Upload to Knowledge Base is running in the background — "
            "refresh this page to see the final status.",
        )

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "upload-zip/",
                self.admin_site.admin_view(self.zip_upload_view),
                name="ask_pdfresource_upload_zip",
            ),
        ]
        return custom + urls

    def zip_upload_view(self, request):
        changelist_url = reverse("admin:ask_pdfresource_changelist")
        filename_col, title_col = self.zip_csv_required_columns
        required_columns = set(self.zip_csv_required_columns)
        required_columns_label = ", ".join(self.zip_csv_required_columns)

        if request.method == "POST":
            zip_file = request.FILES.get("zip_file")
            if not zip_file:
                messages.error(request, "Please select a zip file to upload.")
                return HttpResponseRedirect(request.path)

            try:
                archive = zipfile.ZipFile(zip_file)
            except zipfile.BadZipFile:
                messages.error(request, "The uploaded file is not a valid zip archive.")
                return HttpResponseRedirect(request.path)

            with archive:
                # skip macOS Finder metadata: __MACOSX/ dir and AppleDouble "._" twins
                def _is_real(name):
                    base = os.path.basename(name)
                    return not name.startswith("__MACOSX/") and not base.startswith("._") and base != ""

                real_names = [n for n in archive.namelist() if _is_real(n)]

                csv_names = [n for n in real_names if n.lower().endswith(".csv")]
                if len(csv_names) == 0:
                    messages.error(
                        request,
                        f"Zip must contain one CSV metadata file ({required_columns_label}).",
                    )
                    return HttpResponseRedirect(request.path)
                if len(csv_names) > 1:
                    messages.error(request, f"Zip must contain exactly one CSV; found {len(csv_names)}.")
                    return HttpResponseRedirect(request.path)

                csv_text = archive.read(csv_names[0]).decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(csv_text))
                # strip header names so the column check and per-row lookups use
                # the same keys; otherwise a header like "filename, title" leaves
                # stray spaces and every row reads as missing its required fields
                if reader.fieldnames:
                    reader.fieldnames = [(name or "").strip() for name in reader.fieldnames]
                csv_columns = set(reader.fieldnames or [])
                if not required_columns.issubset(csv_columns):
                    missing = ", ".join(sorted(required_columns - csv_columns))
                    messages.error(request, f"CSV is missing required columns: {missing}.")
                    return HttpResponseRedirect(request.path)

                zip_members = {n: n for n in real_names}
                # also index by basename so CSV can refer to bare filenames regardless of zip layout
                for n in real_names:
                    zip_members.setdefault(os.path.basename(n), n)

                # a PDF already exists when both its original filename and
                # title match a row already imported
                existing_pdfs = set(
                    PDFResource.objects.values_list("original_filename", "title")
                )

                total = 0
                saved = 0
                queued_ids = []
                for row in reader:
                    total += 1
                    filename = (row.get(filename_col) or "").strip()
                    title = (row.get(title_col) or "").strip()
                    if not filename or not title:
                        messages.warning(
                            request,
                            f"Row {total}: missing {filename_col} or {title_col}; skipped.",
                        )
                        continue

                    basename = os.path.basename(filename)
                    if (basename, title) in existing_pdfs:
                        messages.warning(request, f"Row {total}: '{filename}' already exists; skipped.")
                        continue

                    member = zip_members.get(filename) or zip_members.get(basename)
                    if not member:
                        messages.warning(request, f"Row {total}: '{filename}' not in zip; skipped.")
                        continue

                    try:
                        pdf_bytes = archive.read(member)
                    except KeyError:
                        messages.warning(request, f"Row {total}: could not read '{filename}'; skipped.")
                        continue

                    obj = PDFResource(
                        title=title,
                        original_filename=basename,
                        creator=request.user,
                        modifier=request.user,
                        status=PDFResource.Status.PROCESSING,
                        status_message="Queued for Knowledge Base upload.",
                    )
                    for warning in _apply_zip_csv_metadata(obj, row):
                        messages.warning(request, f"Row {total}: {warning}")
                    obj.file.save(basename, ContentFile(pdf_bytes), save=True)
                    saved += 1
                    existing_pdfs.add((basename, title))
                    queued_ids.append(obj.pk)

                # fire KB uploads after the request transaction commits so background
                # threads see the just-saved rows
                def _start_uploads(ids=tuple(queued_ids)):
                    for pk in ids:
                        threading.Thread(
                            target=run_kb_resource_upload,
                            args=("pdf", pk),
                            daemon=True,
                        ).start()
                transaction.on_commit(_start_uploads)

                messages.success(
                    request,
                    f"Imported {saved} of {total} PDFs. Knowledge Base uploads are running in the "
                    "background — refresh the list to see each row's final status.",
                )
                return HttpResponseRedirect(changelist_url)

        return render(
            request,
            "admin/ask/pdfresource/upload_zip.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": "Upload zip of PDFs",
                "changelist_url": changelist_url,
                "required_columns": self.zip_csv_required_columns,
                "required_columns_label": required_columns_label,
            },
        )
