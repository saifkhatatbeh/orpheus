"""THOROUGH party E2E: feed, track-change, seek, pause/resume — verify at every step.

Checks snapserver stream state AND that ffmpeg feeder follows deck actions.
"""
import asyncio, json
import websockets, httpx

RPC = "http://localhost:1780/jsonrpc"
API = "http://localhost:7777/api"
PASS, FAIL = 0, 0

def check(name, ok, detail=""):
    global PASS, FAIL
    print(f"  {'✓' if ok else '✗ FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    PASS, FAIL = PASS + (1 if ok else 0), FAIL + (0 if ok else 1)

async def stream_status(c):
    for attempt in range(3):
        try:
            r = await c.post(RPC, json={"id": 1, "jsonrpc": "2.0", "method": "Server.GetStatus"},
                             headers={"Connection": "close"})
            return r.json()["result"]["server"]["streams"][0]["status"]
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(0.5)

async def party_status(c):
    return (await c.get(f"{API}/party/status")).json()

async def main():
    a = await websockets.connect("ws://localhost:7777/ws")
    await a.send(json.dumps({"type": "hello", "name": "TESTER", "kind": "pc"}))

    async def drain(t=0.8):
        try:
            while True:
                await asyncio.wait_for(a.recv(), t)
        except asyncio.TimeoutError:
            pass

    def send(action, payload):
        return a.send(json.dumps({"type": "action", "action": action, "payload": payload}))

    await drain()
    async with httpx.AsyncClient(timeout=10) as c:

        print("\n[1] baseline")
        st = await party_status(c)
        check("engine up", st["up"])
        check("snapclient connected", any(x["connected"] for x in st["clients"]),
              ", ".join(x["id"] for x in st["clients"]))
        check("not feeding yet", not st["feeding"])

        print("\n[2] play track + party ON  →  feed starts")
        await send("play_track", {"track": {"id": "qe8Q7mjxjig", "title": "Veridis Quo", "artist": "Daft Punk", "duration": 340}})
        await drain()
        await send("party", {"on": True})
        await asyncio.sleep(4)
        st = await party_status(c)
        check("feeding", st["feeding"])
        check("stream playing", (await stream_status(c)) == "playing")

        print("\n[3] track change  →  feeder swaps source")
        await send("play_track", {"track": {"id": "HQnC1UHBvWA", "title": "Something About Us", "artist": "Daft Punk", "duration": 231}})
        await asyncio.sleep(4)
        st = await party_status(c)
        check("still feeding after track change", st["feeding"])
        check("stream still playing", (await stream_status(c)) == "playing")

        print("\n[4] seek  →  feeder restarts at position")
        await send("seek", {"position_s": 120})
        await asyncio.sleep(4)
        st = await party_status(c)
        check("feeding after seek", st["feeding"])

        print("\n[5] pause  →  feed stops;  resume  →  feed returns")
        await send("toggle_play", {})
        await asyncio.sleep(2.5)
        st = await party_status(c)
        check("feed stopped on pause", not st["feeding"])
        await send("toggle_play", {})
        await asyncio.sleep(4)
        st = await party_status(c)
        check("feed resumed", st["feeding"])

        print("\n[6] per-client volume via JSON-RPC")
        cid = st["clients"][0]["id"]
        r = await c.post(f"{API}/party/client/{cid}/volume", json={"percent": 55})
        check("volume set accepted", r.status_code == 200)
        st = await party_status(c)
        check("volume readback = 55", st["clients"][0]["volume"] == 55)
        await c.post(f"{API}/party/client/{cid}/volume", json={"percent": 100})

        print("\n[7] party OFF  →  everything winds down")
        await send("party", {"on": False})
        await asyncio.sleep(2.5)
        st = await party_status(c)
        check("feed stopped", not st["feeding"])
        check("stream back to idle", (await stream_status(c)) == "idle")

    await a.close()
    print(f"\n{'='*46}\nRESULT: {PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)

asyncio.run(main())
