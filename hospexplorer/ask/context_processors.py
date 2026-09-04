from django.conf import settings
from django.urls import reverse
from ask.models import Conversation


def sidebar_conversations(request):
    if request.user.is_authenticated:
        limit = settings.SIDEBAR_CONVERSATIONS_LIMIT
        conversations = Conversation.objects.filter(
            user=request.user
        )[:limit]

        sidebar_items = []
        for conv in conversations:
            label = conv.title if conv.title else conv.created_at.strftime("%b %d, %Y %I:%M %p")
            sidebar_items.append({
                "id": conv.id,
                "label": label,
                "url": reverse("ask:conversation", kwargs={"conversation_id": conv.id}),
                "updated_at": conv.updated_at.isoformat(),
            })
        return {
            "sidebar_conversations": sidebar_items,
            "sidebar_conversations_limit": limit,
        }
    return {"sidebar_conversations": [], "sidebar_conversations_limit": 0}


# Django context processor registered in settings TEMPLATES
# Injects terms_accepted (bool) and terms_version into every template context,
# reads from the session cache set by TermsAcceptanceMiddleware to avoid hitting the db on every request
def terms_status(request):
    if hasattr(request, "user") and request.user.is_authenticated:
        accepted = request.session.get("terms_accepted_version") == settings.TERMS_VERSION
        return {"terms_accepted": accepted, "terms_version": settings.TERMS_VERSION}
    return {}
