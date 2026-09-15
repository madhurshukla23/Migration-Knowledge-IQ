# Copyright (c) Microsoft. All rights reserved.

import os

import ado_client
from agent_framework import Agent, tool
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from pydantic import Field
from typing_extensions import Annotated

# Load environment variables from .env file
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


def main():
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=DefaultAzureCredential(),
    )

    agent = Agent(
        client=client,
        instructions=(
            "You are Knowledge IQ, an assistant that answers questions using this team's Azure DevOps "
            "work items and wiki. Use the search tools to find relevant items or pages before answering, "
            "then use the get tools to pull full details. Always cite the work item id or wiki page path "
            "and url in your answer. If nothing relevant is found, say so instead of guessing."
        ),
        tools=[search_work_items, get_work_item, search_wiki, get_wiki_page],
        # History will be managed by the hosting infrastructure, thus there
        # is no need to store history by the service. Learn more at:
        # https://developers.openai.com/api/reference/resources/responses/methods/create
        default_options={"store": False},
    )

    server = ResponsesHostServer(agent)
    server.run()


if __name__ == "__main__":
    main()
