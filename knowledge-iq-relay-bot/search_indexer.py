# Copyright (c) Microsoft. All rights reserved.
"""Azure AI Search indexer for Azure DevOps work items and wiki pages.

Populates a hybrid (vector + keyword) index so the bot can answer questions
without hitting Azure DevOps live on every question. Embeddings are generated
with the text-embedding-3-small deployment on the same Foundry account used
for chat, authenticated with the Function App's managed identity.
"""

import os

import requests
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery

import ado_client

_INDEX_NAME = "knowledge-iq-index"
_EMBEDDING_DEPLOYMENT = "text-embedding-3-small"
_EMBEDDING_DIMENSIONS = 1536
_CHUNK_SIZE = 3000
_credential = DefaultAzureCredential()


def _search_client() -> SearchClient:
    endpoint = os.environ["AZURE_SEARCH_ENDPOINT"]
    return SearchClient(endpoint=endpoint, index_name=_INDEX_NAME, credential=_credential)


def _embed_text(text: str) -> list[float]:
    """Get an embedding vector for a chunk of text using the Foundry embedding deployment."""
    token = _credential.get_token("https://cognitiveservices.azure.com/.default").token
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    url = f"{endpoint}/openai/deployments/{_EMBEDDING_DEPLOYMENT}/embeddings?api-version=2024-02-01"
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"input": text[:8000]},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["data"][0]["embedding"]


def _chunk_text(text: str, chunk_size: int = _CHUNK_SIZE) -> list[str]:
    """Split long text into roughly chunk_size-character pieces on paragraph boundaries."""
    if not text:
        return []
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 2 > chunk_size and current:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    return chunks


def _upload_documents(documents: list[dict]) -> None:
    if not documents:
        return
    client = _search_client()
    for i in range(0, len(documents), 100):
        client.merge_or_upload_documents(documents=documents[i : i + 100])


def sync_work_items() -> int:
    """Index every work item in the project. Returns the number of chunks indexed."""
    documents = []
    for item in ado_client.list_all_work_items():
        text = f"{item['title']}\n\n{item.get('description') or ''}"
        for chunk_index, chunk in enumerate(_chunk_text(text)):
            documents.append(
                {
                    "id": f"wi-{item['id']}-{chunk_index}",
                    "source": "ado_work_item",
                    "title": item["title"],
                    "url": item["url"],
                    "path": "",
                    "text": chunk,
                    "project": os.environ["ADO_PROJECT"],
                    "text_vector": _embed_text(chunk),
                }
            )
    _upload_documents(documents)
    return len(documents)


def sync_wiki() -> int:
    """Index every wiki page in the project. Returns the number of chunks indexed."""
    documents = []
    for page in ado_client.list_all_wiki_pages():
        for chunk_index, chunk in enumerate(_chunk_text(page.get("content") or "")):
            documents.append(
                {
                    "id": f"wiki-{abs(hash(page['path']))}-{chunk_index}",
                    "source": "ado_wiki",
                    "title": page["path"],
                    "url": page["url"],
                    "path": page["path"],
                    "text": chunk,
                    "project": os.environ["ADO_PROJECT"],
                    "text_vector": _embed_text(chunk),
                }
            )
    _upload_documents(documents)
    return len(documents)


def sync_all() -> dict:
    """Reindex all Azure DevOps work items and wiki pages."""
    work_item_chunks = sync_work_items()
    wiki_chunks = sync_wiki()
    return {"work_item_chunks": work_item_chunks, "wiki_chunks": wiki_chunks}


def search(query_text: str, top: int = 5) -> list[dict]:
    """Hybrid vector plus keyword search over the indexed Azure DevOps content."""
    client = _search_client()
    vector_query = VectorizedQuery(vector=_embed_text(query_text), k_nearest_neighbors=top, fields="text_vector")
    results = client.search(search_text=query_text, vector_queries=[vector_query], top=top)
    return [
        {
            "source": result["source"],
            "title": result["title"],
            "url": result["url"],
            "text": result["text"],
            "score": result["@search.score"],
        }
        for result in results
    ]
