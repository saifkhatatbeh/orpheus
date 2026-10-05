"""Orpheus Constellation — shared player state + node (speaker) registry.

Every connected browser tab (PC → Edifier, phone, iPad) registers as a
*node* over WebSocket. The hub holds one canonical player state and
broadcasts every mutation to all nodes. Exactly one node is `active`
(the one actually emitting sound) unless `party` mode syncs them all.

Sync strategy for party mode: server timestamps every play/seek command
(`epoch_ms` + `position_s`). Nodes compute offset via a ping/pong clock
handshake and schedule their <audio> element accordingly — millisecond-
class alignment, good enough for multi-room.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


def now_ms() -> int:
    return int(time.time() * 1000)


class Node:
    def __init__(self, ws: WebSocket, name: str, kind: str):
        self.ws = ws
        self.id = uuid.uuid4().hex[:8]
        self.name = name
        self.kind = kind  # pc | phone | tablet | other
        self.joined = now_ms()

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind}


class VirtualNode:
    """A speaker Orpheus drives server-side (DLNA renderer like the Music Frame)."""

    def __init__(self, node_id: str, name: str, kind: str, meta: dict):
        self.id = node_id
        self.name = name
        self.kind = kind  # dlna
        self.meta = meta

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind, "virtual": True,
                "device_id": self.meta.get("device_id")}


class Hub:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.virtual: dict[str, VirtualNode] = {}
        self.on_change = []  # async callbacks(action, state)
        self.state: dict[str, Any] = {
            "track": None,          # normalized song dict
            "queue": [],            # upcoming songs
            "history": [],          # played songs (most recent last)
            "playing": False,
            "position_s": 0.0,      # position at epoch_ms
            "epoch_ms": now_ms(),   # server time when position was stamped
            "volume": 0.8,
            "active_node": None,    # node id emitting audio
            "party": False,         # all nodes play in sync (snapcast engine)
            "mode": "deck",         # deck | radio
            "radio_seed": None,     # videoId feeding auto-queue
            "station": None,        # live radio station dict
        }
        self._lock = asyncio.Lock()

    # ---------- broadcast ----------
    async def broadcast(self, msg: dict) -> None:
        dead = []
        data = json.dumps(msg)
        for node in self.nodes.values():
            try:
                await node.ws.send_text(data)
            except Exception:
                dead.append(node.id)
        for nid in dead:
            self.nodes.pop(nid, None)
        if dead:
            await self._nodes_changed()

    async def _nodes_changed(self) -> None:
        # keep active_node valid
        all_ids = set(self.nodes) | set(self.virtual)
        if self.state["active_node"] not in all_ids:
            self.state["active_node"] = next(iter(self.nodes), next(iter(self.virtual), None))
        await self.broadcast({
            "type": "nodes",
            "nodes": self.all_nodes(),
            "active_node": self.state["active_node"],
            "party": self.state["party"],
        })

    def all_nodes(self) -> list[dict]:
        return [n.info() for n in self.nodes.values()] + [v.info() for v in self.virtual.values()]

    def snapshot(self) -> dict:
        return {
            "type": "state",
            "state": self.state,
            "nodes": self.all_nodes(),
            "server_ms": now_ms(),
        }

    # ---------- state mutations ----------
    async def apply(self, action: str, p: dict, sender: Optional[Node]) -> None:
        async with self._lock:
            s = self.state
            if action == "play_track":
                track = p["track"]
                if s["track"]:
                    s["history"] = (s["history"] + [s["track"]])[-50:]
                s["track"] = track
                s["playing"] = True
                s["position_s"] = 0.0
                s["epoch_ms"] = now_ms()
                s["mode"] = p.get("mode", "deck")
                s["station"] = None
                if p.get("queue") is not None:
                    s["queue"] = p["queue"]
                if p.get("radio_seed"):
                    s["radio_seed"] = p["radio_seed"]
            elif action == "queue_set":
                s["queue"] = p.get("queue", [])
            elif action == "queue_add":
                s["queue"] = s["queue"] + [p["track"]]
            elif action == "next":
                if s["queue"]:
                    if s["track"]:
                        s["history"] = (s["history"] + [s["track"]])[-50:]
                    s["track"] = s["queue"].pop(0)
                    s["playing"] = True
                    s["position_s"] = 0.0
                    s["epoch_ms"] = now_ms()
            elif action == "prev":
                if s["history"]:
                    if s["track"]:
                        s["queue"] = [s["track"]] + s["queue"]
                    s["track"] = s["history"].pop()
                    s["playing"] = True
                    s["position_s"] = 0.0
                    s["epoch_ms"] = now_ms()
            elif action == "toggle_play":
                # stamp current position before flipping
                if s["playing"]:
                    elapsed = (now_ms() - s["epoch_ms"]) / 1000
                    s["position_s"] = s["position_s"] + elapsed
                s["playing"] = not s["playing"]
                s["epoch_ms"] = now_ms()
            elif action == "seek":
                s["position_s"] = float(p["position_s"])
                s["epoch_ms"] = now_ms()
            elif action == "position_report":
                # active node reports truth periodically; keeps UI honest
                if sender and sender.id == s["active_node"]:
                    s["position_s"] = float(p["position_s"])
                    s["epoch_ms"] = now_ms()
                    if "playing" in p:
                        s["playing"] = bool(p["playing"])
            elif action == "volume":
                s["volume"] = max(0.0, min(1.0, float(p["volume"])))
            elif action == "set_active":
                if p["node_id"] in self.nodes or p["node_id"] in self.virtual:
                    s["active_node"] = p["node_id"]
                    s["party"] = False
            elif action == "party":
                on = bool(p["on"])
                if on and not s["party"]:
                    # freeze current position and restamp so the party
                    # feeder starts from the deck's exact position
                    if s["playing"]:
                        elapsed = (now_ms() - s["epoch_ms"]) / 1000
                        s["position_s"] = s["position_s"] + elapsed
                    s["epoch_ms"] = now_ms()
                s["party"] = on
            elif action == "mode":
                s["mode"] = p["mode"]
            elif action == "station":
                s["station"] = p["station"]
                s["track"] = None
                s["playing"] = True
                s["mode"] = "radio"
                s["position_s"] = 0.0
                s["epoch_ms"] = now_ms()
            else:
                return
        await self.broadcast({
            "type": "action",
            "action": action,
            "payload": p,
            "state": self.state,
            "server_ms": now_ms(),
            "by": sender.info() if sender else None,
        })
        for cb in self.on_change:
            try:
                await cb(action, self.state)
            except Exception:
                pass


hub = Hub()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    node: Optional[Node] = None
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            t = msg.get("type")
            if t == "hello":
                node = Node(ws, msg.get("name") or "node", msg.get("kind") or "other")
                hub.nodes[node.id] = node
                if hub.state["active_node"] is None:
                    hub.state["active_node"] = node.id
                await ws.send_text(json.dumps({"type": "welcome", "you": node.info()}))
                await ws.send_text(json.dumps(hub.snapshot()))
                await hub._nodes_changed()
            elif t == "ping":
                # clock sync: echo client time + server time
                await ws.send_text(json.dumps({
                    "type": "pong", "client_ms": msg.get("client_ms"), "server_ms": now_ms(),
                }))
            elif t == "action" and node:
                await hub.apply(msg.get("action"), msg.get("payload") or {}, node)
    except WebSocketDisconnect:
        pass
    finally:
        if node:
            hub.nodes.pop(node.id, None)
            await hub._nodes_changed()
