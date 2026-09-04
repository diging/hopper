import httpx
from django.conf import settings
from ask.models import SimWorkflow


def _get_endpoint():
    active = SimWorkflow.get_active(SimWorkflow.WorkflowType.AGENT)
    if active and active.agent_endpoint:
        return active.agent_endpoint
    # if there are no active workflows, use the LLM_HOST from the settings as fallback
    return settings.LLM_HOST


def query_llm(query, llm_conversation_id=None):  # llm_conversation_id is the UUID, not the integer PK
    headers = {
        "X-API-Key": settings.LLM_TOKEN,
        "Content-Type": "application/json",
    }

    payload = {
        "input": query,
        "conversationId": str(llm_conversation_id),
    }

    endpoint = _get_endpoint()

    with httpx.Client() as client:
        response = client.post(
            endpoint,
            json=payload,
            headers=headers,
            timeout=settings.LLM_TIMEOUT
        )

    response.raise_for_status()
    return response.json()
