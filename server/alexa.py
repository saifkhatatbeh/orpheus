"""Orpheus ⇄ Alexa bridge.

Alexa has no sanctioned local API, but the alexa.amazon.com web app's
private REST API works with your login cookies. This is the same trick
alexa-remote-control uses. One-time setup:

  1. Sign in at https://alexa.amazon.com in your browser
  2. DevTools → Network → any /api/* request → copy the `Cookie` header
     value and the `csrf` value within it
  3. POST it to /api/alexa/auth  (stored in alexa_cookie.json, local only)

Once armed, Orpheus can:
  - list your Echo devices
  - make Alexa SPEAK (announcements: "Now playing …")
  - issue TEXT COMMANDS as if spoken ("play Bad Bunny on spotify") —
    which is how provider switching works: Alexa owns the Spotify /
    Apple Music / Amazon Music sessions natively, we just tell it what
    to do. Orpheus = the deck, Alexa = the brain's voice arm.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api")

_STORE = Path(__file__).resolve().parent.parent / "alexa_cookie.json"
_BASE = "https://alexa.amazon.com"


def _load() -> Optional[dict]:
    if _STORE.exists():
        try:
            return json.loads(_STORE.read_text())
        except json.JSONDecodeError:
            return None
    return None


def _client() -> httpx.AsyncClient:
    auth = _load()
    if not auth:
        raise HTTPException(428, "Alexa not linked — POST /api/alexa/auth first (see README)")
    return httpx.AsyncClient(
        base_url=_BASE,
        timeout=15.0,
        headers={
            "Cookie": auth["cookie"],
            "csrf": auth["csrf"],
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": _BASE + "/spa/index.html",
            "Origin": _BASE,
        },
    )


@router.post("/alexa/auth")
async def alexa_auth(payload: dict):
    cookie = (payload.get("cookie") or "").strip()
    if not cookie:
        raise HTTPException(400, "cookie required")
    csrf = payload.get("csrf")
    if not csrf:
        m = re.search(r"csrf=([^;]+)", cookie)
        if not m:
            raise HTTPException(400, "csrf not found in cookie; pass it explicitly")
        csrf = m.group(1)
    _STORE.write_text(json.dumps({"cookie": cookie, "csrf": csrf}))
    # verify immediately
    try:
        devices = await alexa_devices()
        return {"ok": True, "devices": devices["devices"]}
    except HTTPException:
        _STORE.unlink(missing_ok=True)
        raise HTTPException(401, "cookie rejected by Amazon — re-copy it fresh")


@router.get("/alexa/status")
async def alexa_status():
    return {"linked": _load() is not None}


@router.get("/alexa/devices")
async def alexa_devices() -> dict[str, Any]:
    async with _client() as c:
        r = await c.get("/api/devices-v2/device", params={"cached": "false"})
        if r.status_code in (401, 403):
            raise HTTPException(401, "Alexa cookie expired — re-link")
        r.raise_for_status()
        devs = [
            {
                "name": d.get("accountName"),
                "type": d.get("deviceType"),
                "serial": d.get("serialNumber"),
                "family": d.get("deviceFamily"),
                "online": d.get("online", False),
            }
            for d in r.json().get("devices", [])
            if d.get("capabilities") and "AUDIO_PLAYER" in (d.get("capabilities") or [])
        ]
        return {"devices": devs}


async def _behavior(c: httpx.AsyncClient, device: dict, sequence_node: dict) -> None:
    seq = {
        "@type": "com.amazon.alexa.behaviors.model.Sequence",
        "startNode": sequence_node,
    }
    payload = {
        "behaviorId": "PREVIEW",
        "sequenceJson": json.dumps(seq),
        "status": "ENABLED",
    }
    r = await c.post("/api/behaviors/preview", json=payload)
    if r.status_code in (401, 403):
        raise HTTPException(401, "Alexa cookie expired — re-link")
    r.raise_for_status()


def _op(device: dict, op_type: str, extra: dict) -> dict:
    return {
        "@type": "com.amazon.alexa.behaviors.model.OpaquePayloadOperationNode",
        "type": op_type,
        "operationPayload": {
            "deviceType": device["type"],
            "deviceSerialNumber": device["serial"],
            "locale": "en-US",
            "customerId": None,
            **extra,
        },
    }


async def _find_device(name: Optional[str]) -> dict:
    devs = (await alexa_devices())["devices"]
    if not devs:
        raise HTTPException(404, "no Alexa audio devices on account")
    if name:
        for d in devs:
            if d["name"] and name.lower() in d["name"].lower():
                return d
    return devs[0]


@router.post("/alexa/speak")
async def alexa_speak(payload: dict):
    """{text, device?} — Alexa announces through the Echo."""
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    dev = await _find_device(payload.get("device"))
    async with _client() as c:
        await _behavior(c, dev, _op(dev, "Alexa.Speak", {"textToSpeak": text[:250]}))
    return {"ok": True, "device": dev["name"]}


@router.post("/alexa/command")
async def alexa_command(payload: dict):
    """{text, device?} — inject a TEXT command as if you spoke it.

    This is the provider switcher: 'play Money Trees by Kendrick Lamar
    on apple music', 'play my discover weekly on spotify', 'set volume
    to six', 'stop'. Amazon routes it to the right provider session.
    """
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    dev = await _find_device(payload.get("device"))
    async with _client() as c:
        await _behavior(c, dev, _op(dev, "Alexa.TextCommand", {"text": text[:280]}))
    return {"ok": True, "device": dev["name"], "sent": text}
