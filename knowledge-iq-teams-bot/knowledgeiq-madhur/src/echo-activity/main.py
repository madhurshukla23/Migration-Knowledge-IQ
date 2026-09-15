# Copyright (c) Microsoft. All rights reserved.

"""Knowledge IQ Teams agent using the Activity protocol.

Answers Teams chat messages using this team's Azure DevOps work items and wiki,
via the same Agent Framework tool set as the Responses-protocol agent. Hosted by
``azure-ai-agentserver-activity`` for the Foundry platform contract and bridged
to the M365 Agents SDK for activity processing and outbound channel delivery
(Microsoft Teams).
"""

import logging
import os

# Configure logging first
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
logger = logging.getLogger("knowledge-iq-teams-agent")

import ado_client
from agent_framework import Agent, AgentSession, tool
from agent_framework.foundry import FoundryChatClient
from azure.ai.agentserver.activity import ActivityAgentServerHost
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from pydantic import Field
from typing_extensions import Annotated

load_dotenv()


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


_client = FoundryChatClient(
    project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
    model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
    credential=DefaultAzureCredential(),
)

_agent = Agent(
    client=_client,
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

host = ActivityAgentServerHost()
app = host.agent_app


@app.activity("message")
async def on_message(context, state):
    """Answer the user's question using the Azure DevOps tools."""
    user_text = (context.activity.text or "").strip()
    if not user_text:
        return

    conversation_id = context.activity.conversation.id
    session = _sessions.setdefault(conversation_id, AgentSession(session_id=conversation_id))

    try:
        response = await _agent.run(user_text, session=session)
        reply = response.text or "I couldn't come up with an answer for that."
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error("[ERROR] Agent run failed: %s", exc, exc_info=True)
        reply = f"Sorry, I hit an error answering that: {exc}"

    # Outbound delivery goes to the Bot Connector (serviceUrl). Guard it so a
    # transient delivery failure is logged instead of surfacing as a 500 on
    # the inbound webhook (which would make the Bot Connector retry).
    try:
        await context.send_activity(reply)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("[ERROR] Could not send reply: %s", exc)


@app.activity("conversationUpdate")
async def on_members_added(context, state):
    """Welcome new members."""
    members = context.activity.members_added or []
    for member in members:
        if member.id != context.activity.recipient.id:
            try:
                await context.send_activity(
                    "Hi! I'm Knowledge IQ. Ask me about Azure DevOps work items or wiki pages."
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning("[ERROR] Could not send welcome: %s", exc)


@app.error
async def on_error(context, error):
    """Handle unhandled errors."""
    logger.error("[ERROR] HANDLER ERROR | error=%s", error, exc_info=True)
    await context.send_activity(f"Sorry, something went wrong: {error}")


if __name__ == "__main__":
    logger.info("Starting Knowledge IQ Teams agent ...")
    host.run()
