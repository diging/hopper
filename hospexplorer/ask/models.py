import uuid

from django.conf import settings
from django.db import models

# Abstract Model, fields are inherited by subclasses
class Resource(models.Model):
    class Status(models.TextChoices):
        PROCESSING = "processing", "Processing"
        SUCCESS = "success", "Success"
        ERROR = "error", "Error"
        WARNING = "warning", "Warning"

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="%(class)s_created",
    )
    modifier = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="%(class)s_modified",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.SUCCESS,
    )
    status_message = models.TextField(blank=True, default="")

    class Meta:
        abstract = True

    def __str__(self):
        return self.title


class WebsiteResource(Resource):
    url = models.URLField()
    mcp_kb_document_id = models.IntegerField(null=True, blank=True, help_text="Document ID returned by the MCP Knowledge Base.")

    class Meta:
        verbose_name = "Website Resource"
        verbose_name_plural = "Website Resources"


class PDFResource(Resource):
    file = models.FileField(upload_to="kb_pdfs/")
    mcp_kb_document_id = models.IntegerField(null=True, blank=True, help_text="Document ID returned by the MCP Knowledge Base.")

    class Meta:
        verbose_name = "PDF Resource"
        verbose_name_plural = "PDF Resources"


class QueryTask(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="query_tasks",
    )
    query_text = models.TextField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    result = models.TextField(blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Conversation(models.Model):
    # UUID sent to the LLM backend to identify this conversation (separate from the integer PK used in URLs)
    llm_conversation_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="conversations",
    )
    title = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        if self.title:
            truncated = self.title[:50]
            suffix = "..." if len(self.title) > 50 else ""
            return f"Conversation {self.id}: {truncated}{suffix}"
        return f"Conversation {self.id} ({self.user.username})"


class TermsAcceptance(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="terms_acceptances",
    )
    terms_version = models.CharField(max_length=20)
    accepted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-accepted_at"]
        indexes = [
            models.Index(fields=["user", "terms_version"]),
        ]

    def __str__(self):
        return f"{self.user.username} accepted v{self.terms_version} on {self.accepted_at}"


class SimWorkflow(models.Model):
    class WorkflowType(models.TextChoices):
        AGENT = "agent", "Agent"

    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    workflow_id = models.CharField(max_length=255)
    agent_endpoint = models.URLField(max_length=500, blank=True, default="")
    is_active = models.BooleanField(default=False)
    workflow_type = models.CharField(
        max_length=20,
        choices=WorkflowType.choices,
        default=WorkflowType.AGENT,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.title} ({self.workflow_id})"

    @classmethod
    def get_active(cls, workflow_type):
        return cls.objects.filter(is_active=True, workflow_type=workflow_type).first()

    def save(self, *args, **kwargs):
        # constraint: only one workflow per type can be active.
        # activating this workflow automatically deactivates others of the same type.
        if self.is_active:
            SimWorkflow.objects.exclude(pk=self.pk).filter(
                is_active=True, workflow_type=self.workflow_type
            ).update(is_active=False)
        # constraint: at least one workflow per type must remain active
        elif self.pk and not SimWorkflow.objects.exclude(pk=self.pk).filter(
            is_active=True, workflow_type=self.workflow_type
        ).exists():
            from django.core.exceptions import ValidationError
            raise ValidationError("Cannot deactivate the only active workflow of this type. Activate another one first.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # constraint: cannot delete the sole active workflow of its type, activate another one first
        if self.is_active and not SimWorkflow.objects.exclude(pk=self.pk).filter(
            is_active=True, workflow_type=self.workflow_type
        ).exists():
            from django.core.exceptions import ValidationError
            raise ValidationError("Cannot delete the only active workflow of this type. Activate another one first.")
        super().delete(*args, **kwargs)


class QARecord(models.Model):
    """
    Stores a question-answer pair from user interactions with the LLM.
    """
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="qa_records",
    )

    # Question fields
    question_text = models.TextField()
    question_timestamp = models.DateTimeField(auto_now_add=True)

    # Answer fields
    answer_text = models.TextField(blank=True, default="")
    answer_raw_response = models.JSONField(default=dict)
    answer_timestamp = models.DateTimeField(null=True, blank=True)
    is_error = models.BooleanField(default=False)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="qa_records",
    )

    class Meta:
        ordering = ["-question_timestamp"]
        verbose_name = "Q&A Record"
        verbose_name_plural = "Q&A Records"
        indexes = [
            models.Index(fields=["user", "-question_timestamp"]),
        ]

    def __str__(self):
        truncated = self.question_text[:50]
        suffix = "..." if len(self.question_text) > 50 else ""
        return f"{self.user.username}: {truncated}{suffix}"
