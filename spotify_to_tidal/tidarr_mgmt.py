"""Tidarr REST API helpers.

All functions accept an AppConfig instance and issue requests against
cfg.tidarr_url using the X-Api-Key header from cfg.tidarr_api_key.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from .config import AppConfig


def _headers(cfg: "AppConfig") -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if cfg.tidarr_api_key:
        h["X-Api-Key"] = cfg.tidarr_api_key
    return h


def _base(cfg: "AppConfig") -> str:
    return cfg.tidarr_url.rstrip("/")


# ---------------------------------------------------------------------------
# Sync management
# ---------------------------------------------------------------------------

def list_sync(cfg: "AppConfig") -> list[dict]:
    """GET /api/sync/list — returns list of sync items."""
    resp = requests.get(
        f"{_base(cfg)}/api/sync/list",
        headers=_headers(cfg),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("items", data.get("data", []))


def add_sync(cfg: "AppConfig", url: str, title: str, type_: str = "playlist") -> bool:
    item_id = uuid.uuid4().hex
    payload = {"item": {"id": item_id, "title": title, "url": url, "type": type_}}
    resp = requests.post(
        f"{_base(cfg)}/api/sync/save",
        json=payload,
        headers=_headers(cfg),
        timeout=15,
    )
    resp.raise_for_status()
    return True


def remove_sync(cfg: "AppConfig", item_id: str) -> bool:
    """DELETE /api/sync/remove — remove a sync item by id."""
    resp = requests.delete(
        f"{_base(cfg)}/api/sync/remove",
        json={"id": item_id},
        headers=_headers(cfg),
        timeout=15,
    )
    resp.raise_for_status()
    return True


def trigger_sync(cfg: "AppConfig") -> bool:
    """POST /api/sync/trigger — trigger an immediate sync run."""
    resp = requests.post(
        f"{_base(cfg)}/api/sync/trigger",
        headers=_headers(cfg),
        timeout=15,
    )
    resp.raise_for_status()
    return True


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def list_history(cfg: "AppConfig") -> list[Any]:
    """GET /api/history/list — returns list of history entries."""
    resp = requests.get(
        f"{_base(cfg)}/api/history/list",
        headers=_headers(cfg),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("items", data.get("data", []))


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

def list_queue(cfg: "AppConfig", offset: int = 0, limit: int = 100) -> dict:
    """GET /api/queue/list — returns paginated queue state."""
    resp = requests.get(
        f"{_base(cfg)}/api/queue/list",
        params={"offset": offset, "limit": limit},
        headers=_headers(cfg),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def clear_queue(cfg: "AppConfig") -> bool:
    """DELETE /api/remove-all — clear the entire download queue."""
    resp = requests.delete(
        f"{_base(cfg)}/api/remove-all",
        headers=_headers(cfg),
        timeout=15,
    )
    resp.raise_for_status()
    return True
