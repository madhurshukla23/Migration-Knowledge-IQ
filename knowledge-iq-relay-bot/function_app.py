# Copyright (c) Microsoft. All rights reserved.
"""Teams relay bot: classic Bot Framework auth, forwards questions to the same
Azure DevOps knowledge tools used by the Foundry hosted agents."""

import logging
import os

import azure.functions as func
from agent_framework import Agent, AgentSession, tool
from agent_framework.foundry import FoundryChatClient
from azure.identity import DefaultAzureCredential
from botbuilder.schema import Activity, ActivityTypes
from botframework.connector import ConnectorClient
from botframework.connector.auth import (
    JwtTokenValidation,
    MicrosoftAppCredentials,
    SimpleCredentialProvider,
)
from pydantic import Field
from typing_extensions import Annotated

import ado_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("knowledge-iq-relay-bot")

APP_ID = os.environ["MicrosoftAppId"]
APP_PASSWORD = os.environ["MicrosoftAppPassword"]
# The Azure Bot resource is registered as SingleTenant, so outbound reply tokens must be
# minted against our own tenant rather than the multi-tenant "botframework.com" auth tenant
# (the default), or Bot Connector rejects them with 401 Unauthorized.
APP_TENANT_ID = os.environ["MicrosoftAppTenantId"]
_credential_provider = SimpleCredentialProvider(APP_ID, APP_PASSWORD)
_app_credentials = MicrosoftAppCredentials(APP_ID, APP_PASSWORD, channel_auth_tenant=APP_TENANT_ID)


@tool(approval_mode="never_require")
def search_work_items(
    query: Annotated[str, Field(description="Keywords to search for in work item titles and descriptions.")],
) -> str:
    """Search Azure DevOps work items by keyword and return matching id, type, title, state, and url."""
    results = ado_client.search_work_items(query)
    if not results:
        return "No work items matched that query."
    return "\n".join(
        f"#{item['id']} [{item['type']}] {item['title']} ({item['state']}) - {item['url']}" for item in results
    )


@tool(approval_mode="never_require")
def get_work_item(
    work_item_id: Annotated[int, Field(description="The numeric Azure DevOps work item id.")],
) -> str:
    """Get full details, including description, for a specific Azure DevOps work item id."""
    item = ado_client.get_work_item(work_item_id)
    return (
        f"#{item['id']} [{item['type']}] {item['title']}\n"
        f"State: {item['state']}  Assigned to: {item['assigned_to']}\n"
        f"Description: {item['description']}\n"
        f"Url: {item['url']}"
    )


@tool(approval_mode="never_require")
def search_wiki(
    query: Annotated[str, Field(description="Keywords to search for across the Azure DevOps project wiki.")],
) -> str:
    """Full-text search the Azure DevOps project wiki and return matching page paths."""
    results = ado_client.search_wiki(query)
    if not results:
        return "No wiki pages matched that query."
    return "\n".join(f"{item['wiki']}: {item['path']}" for item in results)


@tool(approval_mode="never_require")
def get_wiki_page(
    path: Annotated[str, Field(description="The wiki page path, e.g. /Home or /Team/Runbook.")],
) -> str:
    """Get the content of an Azure DevOps wiki page at the given path."""
    page = ado_client.get_wiki_page(path)
    return f"{page['path']} ({page['url']}):\n{page['content']}"


_chat_client = FoundryChatClient(
    project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
    model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
    credential=DefaultAzureCredential(),
)

_agent = Agent(
    client=_chat_client,
    instructions=(
        "You are Knowledge IQ, an assistant that answers questions using this team's Azure DevOps "
        "work items and wiki. Use the search tools to find relevant items or pages before answering, "
        "then use the get tools to pull full details. Always cite the work item id or wiki page path "
        "and url in your answer. If nothing relevant is found, say so instead of guessing. Keep Teams "
        "replies concise."
    ),
    tools=[search_work_items, get_work_item, search_wiki, get_wiki_page],
    default_options={"store": False},
)

# In-memory session store keyed by Teams conversation id.
# WARNING: lost on restart. Use durable storage (Redis, Cosmos DB, etc.) in production.
_sessions: dict[str, AgentSession] = {}

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


def _send_reply(activity: Activity, text: str) -> None:
    reply = Activity(
        type=ActivityTypes.message,
        text=text,
        from_property=activity.recipient,
        recipient=activity.from_property,
        conversation=activity.conversation,
        reply_to_id=activity.id,
        service_url=activity.service_url,
    )
    MicrosoftAppCredentials.trust_service_url(activity.service_url)
    connector = ConnectorClient(_app_credentials, base_url=activity.service_url)
    connector.conversations.send_to_conversation(activity.conversation.id, reply)


@app.route(route="messages", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
async def messages(req: func.HttpRequest) -> func.HttpResponse:
    """Bot Framework messaging endpoint."""
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(status_code=400)

    activity = Activity().deserialize(body)
    auth_header = req.headers.get("Authorization", "")

    try:
        await JwtTokenValidation.authenticate_request(activity, auth_header, _credential_provider, "")
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error("[AUTH] Rejected inbound activity: %s", exc)
        return func.HttpResponse(status_code=401)

    try:
        if activity.type == ActivityTypes.message:
            user_text = (activity.text or "").strip()
            if user_text:
                conversation_id = activity.conversation.id
                session = _sessions.setdefault(conversation_id, AgentSession(session_id=conversation_id))
                try:
                    response = await _agent.run(user_text, session=session)
                    reply_text = response.text or "I couldn't come up with an answer for that."
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    logger.error("[ERROR] Agent run failed: %s", exc, exc_info=True)
                    reply_text = f"Sorry, I hit an error answering that: {exc}"
                _send_reply(activity, reply_text)
        elif activity.type == ActivityTypes.conversation_update:
            for member in activity.members_added or []:
                if member.id != activity.recipient.id:
                    _send_reply(activity, "Hi! I'm Knowledge IQ. Ask me about Azure DevOps work items or wiki pages.")
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error("[ERROR] Unhandled error processing activity: %s", exc, exc_info=True)

    return func.HttpResponse(status_code=200)
