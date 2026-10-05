"""E2E for the 4-band sky: feed → all 4 streams playing; band switch mid-play."""
import asyncio, json
import websockets, httpx

RPC = "http://localhost:1780/jsonrpc"
API = "http://localhost:7777/api"
PASS = FAIL = 0

def check(name, ok, detail=""):
    global PASS, FAIL
    print(f"  {'✓' if ok else '✗ FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    PASS, FAIL = PASS + bool(ok), FAIL + (not ok)

async def streams(c):
    for attempt in range(3):
        try:
            r = await c.post(RPC, json={"id": 1, "jsonrpc": "2.0", "method": "Server.GetStatus"},
                             headers={"Connection": "close"})
            return {s["id"]: s["status"] for s in r.json()["result"]["server"]["streams"]}
        except Exception:
            if attempt == 2: raise
            await asyncio.sleep(0.5)

async def main():
    a = await websockets.connect("ws://localhost:7777/ws")
    await a.send(json.dumps({"type": "hello", "name": "TESTER", "kind": "pc"}))
    async def drain(t=0.8):
        try:
            while True: await asyncio.wait_for(a.recv(), t)
        except asyncio.TimeoutError: pass
    def send(action, payload):
        return a.send(json.dumps({"type": "action", "action": action, "payload": payload}))

    await drain()
    async with httpx.AsyncClient(timeout=10) as c:
        print("\n[1] play + party ON → ALL FOUR band streams play")
        first = (await c.get(f"{API}/files?limit=1")).json()["tracks"]
        assert first, "put at least one audio file in the library folder and rescan before running this test"
        await send("play_track", {"track": first[0]})
        await drain()
        await send("party", {"on": True})
        await asyncio.sleep(5)
        st = await streams(c)
        for band in ("FULL", "THUMP", "HEART", "AIR"):
            check(f"{band} playing", st.get(band) == "playing", st.get(band))

        print("\n[2] band switching mid-play")
        r = await c.post(f"{API}/party/client/EDIFIER-PC/band", json={"band": "AIR"})
        check("switch EDIFIER-PC → AIR", r.status_code == 200)
        ps = (await c.get(f"{API}/party/status")).json()
        me = next(x for x in ps["clients"] if x["id"] == "EDIFIER-PC")
        check("readback band == AIR", me["band"] == "AIR")
        r = await c.post(f"{API}/party/client/EDIFIER-PC/band", json={"band": "FULL"})
        check("switch back → FULL", r.status_code == 200)

        print("\n[3] bad band rejected")
        r = await c.post(f"{API}/party/client/EDIFIER-PC/band", json={"band": "WOBBLE"})
        check("WOBBLE → 400", r.status_code == 400)

        print("\n[4] party OFF → all four streams idle")
        await send("party", {"on": False})
        await asyncio.sleep(6)
        st = await streams(c)
        for band in ("FULL", "THUMP", "HEART", "AIR"):
            check(f"{band} idle", st.get(band) == "idle", st.get(band))

    await a.close()
    print(f"\n{'='*46}\nRESULT: {PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)

asyncio.run(main())
