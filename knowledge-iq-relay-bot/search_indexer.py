# Copyright (c) Microsoft. All rights reserved.
"""Azure AI Search indexer for Azure DevOps work items and wiki pages.

Populates a hybrid (vector + keyword) index so the bot can answer questions
without hitting Azure DevOps live on every question. Embeddings are generated
with the text-embedding-3-small deployment on the same Foundry account used
for chat, authenticated with the Function App's managed identity.
"""

import hashlib
import os

import requests
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery

import ado_client
import model_retry

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

    def send_request() -> requests.Response:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"input": text[:8000]},
            timeout=30,
        )
        response.raise_for_status()
        return response

    response = model_retry.run_with_rate_limit_retry_sync(send_request)
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


def _prune_stale_documents(source: str, valid_ids: set[str]) -> int:
    """Delete indexed documents for a source that no longer correspond to current content."""
    client = _search_client()
    existing_ids = {
        result["id"]
        for result in client.search(search_text="*", filter=f"source eq '{source}'", select=["id"], top=100000)
    }
    stale_ids = existing_ids - valid_ids
    if not stale_ids:
        return 0
    client.delete_documents(documents=[{"id": stale_id} for stale_id in stale_ids])
    return len(stale_ids)


def _stable_id(path: str) -> str:
    """A doc-id-safe hash that is stable across processes (unlike Python's built-in hash())."""
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def sync_work_items() -> int:
    """Index every work item in the project. Returns the number of chunks indexed."""
    documents = []
    valid_ids: set[str] = set()
    for item in ado_client.list_all_work_items():
        text = f"{item['title']}\n\n{item.get('description') or ''}"
        for chunk_index, chunk in enumerate(_chunk_text(text)):
            doc_id = f"wi-{item['id']}-{chunk_index}"
            valid_ids.add(doc_id)
            documents.append(
                {
                    "id": doc_id,
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
    _prune_stale_documents("ado_work_item", valid_ids)
    return len(documents)


def sync_wiki() -> int:
    """Index every wiki page in the project. Returns the number of chunks indexed."""
    documents = []
    valid_ids: set[str] = set()
    for page in ado_client.list_all_wiki_pages():
        for chunk_index, chunk in enumerate(_chunk_text(page.get("content") or "")):
            doc_id = f"wiki-{_stable_id(page['path'])}-{chunk_index}"
            valid_ids.add(doc_id)
            documents.append(
                {
                    "id": doc_id,
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
    _prune_stale_documents("ado_wiki", valid_ids)
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
