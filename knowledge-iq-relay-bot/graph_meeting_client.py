# Copyright (c) Microsoft. All rights reserved.
"""Delegated Microsoft Graph access for reading a user's own meeting transcripts.

Uses the OAuth device code flow against Microsoft's first-party "Microsoft Graph
Command Line Tools" client, so no application access policy or Teams Administrator
role is required. Only a one-time Entra admin consent for OnlineMeetingTranscript.Read.All
is needed, which a tenant admin can grant themselves.
"""

import re

import requests

_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
_SCOPE = "offline_access https://graph.microsoft.com/OnlineMeetingTranscript.Read.All"
_DEVICE_CODE_URL = "https://login.microsoftonline.com/organizations/oauth2/v2.0/devicecode"
_TOKEN_URL = "https://login.microsoftonline.com/organizations/oauth2/v2.0/token"
_GRAPH_URL = "https://graph.microsoft.com/v1.0"


def start_device_code() -> dict:
    """Request a device code the user can complete sign-in with in a browser."""
    response = requests.post(
        _DEVICE_CODE_URL,
        data={"client_id": _CLIENT_ID, "scope": _SCOPE},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def try_redeem_device_code(device_code: str) -> dict:
    """Attempt one token exchange for a pending device code.

    Returns {"status": "success", "access_token": ...} once the user has signed in,
    {"status": "pending"} while still waiting, or {"status": "error", "error": ...}
    if the code expired or was denied.
    """
    response = requests.post(
        _TOKEN_URL,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": _CLIENT_ID,
            "device_code": device_code,
        },
        timeout=30,
    )
    if response.status_code == 200:
        return {"status": "success", "access_token": response.json()["access_token"]}
    error = response.json().get("error", "unknown_error")
    if error == "authorization_pending":
        return {"status": "pending"}
    return {"status": "error", "error": error}


def get_online_meeting_by_join_url(access_token: str, join_url: str) -> dict:
    """Resolve a Teams meeting join URL to its online meeting id."""
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"$filter": f"JoinWebUrl eq '{join_url}'"}
    response = requests.get(f"{_GRAPH_URL}/me/onlineMeetings", headers=headers, params=params, timeout=30)
    response.raise_for_status()
    meetings = response.json().get("value", [])
    if not meetings:
        raise ValueError("No online meeting found for that join URL. Only meetings you organized can be read.")
    return meetings[0]


def get_latest_transcript_text(access_token: str, online_meeting_id: str) -> str:
    """Fetch the plain-text content of the most recent transcript for a meeting."""
    headers = {"Authorization": f"Bearer {access_token}"}
    list_url = f"{_GRAPH_URL}/me/onlineMeetings/{online_meeting_id}/transcripts"
    response = requests.get(list_url, headers=headers, timeout=30)
    response.raise_for_status()
    transcripts = response.json().get("value", [])
    if not transcripts:
        raise ValueError("No transcript is available for this meeting yet. Transcription must be enabled and the meeting must have ended.")
    transcript_id = transcripts[0]["id"]
    content_url = f"{list_url}/{transcript_id}/content"
    content_response = requests.get(content_url, headers={**headers, "Accept": "text/vtt"}, timeout=30)
    content_response.raise_for_status()
    return content_response.text


_VTT_TIMING_LINE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3}\s*-->")
_VTT_SPEAKER_TAG = re.compile(r"<v\s+([^>]+)>(.*?)(</v>)?$")


def vtt_to_text(vtt_content: str) -> str:
    """Convert a WebVTT transcript into plain speaker-attributed text lines."""
    lines: list[str] = []
    for raw_line in vtt_content.splitlines():
        line = raw_line.strip()
        if not line or line == "WEBVTT" or line.isdigit() or _VTT_TIMING_LINE.match(line):
            continue
        speaker_match = _VTT_SPEAKER_TAG.match(line)
        if speaker_match:
            speaker, text = speaker_match.group(1), speaker_match.group(2)
            lines.append(f"{speaker.strip()}: {text.strip()}")
        else:
            lines.append(line)
    return "\n".join(lines)
