"""Orpheus Guest Pass — QR jukebox for anyone on your WiFi.

Scan the QR on the host screen → minimal jukebox page → search & queue.
No app, no account. Guests can only *add to queue* (rate-limited),
never skip/stop — the host stays in control.
"""
from __future__ import annotations

import socket
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .constellation import hub

router = APIRouter()

_last_add: dict[str, list[float]] = {}  # ip -> timestamps
RATE = 4          # adds
WINDOW = 60.0     # per seconds


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


@router.get("/api/guest/qr")
async def guest_qr():
    import segno

    url = f"http://{lan_ip()}:7777/guest"
    qr = segno.make(url, error="m")
    svg = qr.svg_inline(scale=6, dark="#EDFF45", light=None)
    return {"url": url, "svg": svg}


@router.post("/api/guest/queue")
async def guest_queue(payload: dict, request: Request):
    ip = request.client.host if request.client else "?"
    now = time.time()
    hits = [t for t in _last_add.get(ip, []) if now - t < WINDOW]
    if len(hits) >= RATE:
        raise HTTPException(429, "easy there — max 4 songs a minute")
    track = payload.get("track") or {}
    if not track.get("id"):
        raise HTTPException(400, "track required")
    track["guest"] = True
    hits.append(now)
    _last_add[ip] = hits
    await hub.apply("queue_add", {"track": track}, None)
    return {"ok": True, "position": len(hub.state["queue"])}


_GUEST_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Orpheus · Guest Pass</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono&display=swap" rel="stylesheet"/>
<style>
:root{--blue:#0000F2;--ink:#F5F5F5;--acid:#EDFF45}
*{box-sizing:border-box;margin:0}
body{background:var(--blue);color:var(--ink);font-family:"Space Mono",monospace;min-height:100vh;padding:1.2rem}
h1{font-size:1rem;letter-spacing:.3em;text-transform:uppercase}
h1 b{color:var(--acid)}
p.sub{opacity:.6;font-size:.72rem;margin:.4rem 0 1.4rem}
input{width:100%;padding:.9rem 1rem;background:transparent;border:2px solid rgba(245,245,245,.35);color:var(--ink);font:inherit;font-size:1rem;outline:none}
input:focus{border-color:var(--acid)}
.r{display:flex;gap:.8rem;align-items:center;padding:.7rem 0;border-bottom:1px solid rgba(245,245,245,.15)}
.r img{width:48px;height:48px;object-fit:cover}
.r .t{font-size:.85rem}
.r .a{font-size:.7rem;opacity:.6}
.r button{margin-left:auto;background:var(--acid);color:#000;border:none;font:inherit;font-weight:700;padding:.5rem .9rem;cursor:pointer}
#msg{position:fixed;bottom:1rem;left:50%;transform:translateX(-50%);background:var(--acid);color:#000;padding:.5rem 1.2rem;font-size:.8rem;display:none}
</style></head><body>
<h1>ORPHEUS <b>GUEST PASS</b></h1>
<p class="sub">queue a song · host keeps the wheel · 4/min max</p>
<input id="q" type="search" placeholder="search songs…" autofocus/>
<div id="res"></div>
<div id="msg"></div>
<script>
const q=document.getElementById('q'),res=document.getElementById('res'),msg=document.getElementById('msg');
let tm;q.addEventListener('input',()=>{clearTimeout(tm);tm=setTimeout(go,350)});
async function go(){
  if(!q.value.trim())return res.innerHTML='';
  const d=await fetch('/api/search?q='+encodeURIComponent(q.value)+'&kind=songs').then(r=>r.json());
  res.innerHTML='';
  d.results.filter(t=>t.id).forEach(t=>{
    const el=document.createElement('div');el.className='r';
    el.innerHTML=`<img src="${t.art||''}"/><div><div class="t"></div><div class="a"></div></div><button>+ QUEUE</button>`;
    el.querySelector('.t').textContent=t.title;el.querySelector('.a').textContent=t.artist;
    el.querySelector('button').onclick=async()=>{
      const r=await fetch('/api/guest/queue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({track:t})});
      const j=await r.json().catch(()=>({}));
      show(r.ok?('queued · #'+j.position):(j.detail||'rate limited'));
    };
    res.appendChild(el);
  });
}
function show(t){msg.textContent=t;msg.style.display='block';clearTimeout(show._h);show._h=setTimeout(()=>msg.style.display='none',2200)}
</script></body></html>"""


@router.get("/guest")
async def guest_page():
    return HTMLResponse(_GUEST_HTML)
