# Copyright (c) Microsoft. All rights reserved.
"""Table Storage-backed state for the in-progress 'summarize meeting' device code flow.

Reuses the Function App's existing storage account (AzureWebJobsStorage connection
string), so no new Azure resource or permission grant is required.
"""

import os

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableServiceClient

_TABLE_NAME = "PendingMeetingAuth"
_PARTITION_KEY = "pending"


def _table_client():
    connection_string = os.environ["AzureWebJobsStorage"]
    service = TableServiceClient.from_connection_string(connection_string)
    service.create_table_if_not_exists(_TABLE_NAME)
    return service.get_table_client(_TABLE_NAME)


def save_pending(conversation_id: str, device_code: str, join_url: str) -> None:
    """Persist the in-progress device code sign-in for a conversation."""
    _table_client().upsert_entity(
        {
            "PartitionKey": _PARTITION_KEY,
            "RowKey": conversation_id,
            "device_code": device_code,
            "join_url": join_url,
        }
    )


def get_pending(conversation_id: str) -> dict | None:
    """Look up the pending device code sign-in for a conversation, if any."""
    try:
        entity = _table_client().get_entity(_PARTITION_KEY, conversation_id)
    except ResourceNotFoundError:
        return None
    return {"device_code": entity["device_code"], "join_url": entity["join_url"]}


def delete_pending(conversation_id: str) -> None:
    """Clear the pending device code sign-in for a conversation."""
    try:
        _table_client().delete_entity(_PARTITION_KEY, conversation_id)
    except ResourceNotFoundError:
        pass
