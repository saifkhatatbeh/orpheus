"""One-shot setup for the Orpheus party engine on Windows.

Downloads what the repo deliberately does NOT vendor:
  * snapclient (native Windows client, ~1ms sync)  -> party/snapclient/
  * snapweb    (browser client bundled with snapserver) -> party/snapweb/

Both come from the official badaix/snapcast GitHub releases. Idempotent:
re-running skips anything already present.

    python party/setup_party.py
"""
from __future__ import annotations

import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
RELEASES = "https://api.github.com/repos/badaix/snapcast/releases/latest"
SNAPWEB_RELEASES = "https://api.github.com/repos/badaix/snapweb/releases/latest"
UA = {"User-Agent": "orpheus-setup"}


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _asset(release_api: str, match: str) -> tuple[str, str]:
    rel = json.loads(_get(release_api))
    for a in rel["assets"]:
        if match in a["name"].lower():
            return a["browser_download_url"], rel["tag_name"]
    raise SystemExit(f"no asset matching {match!r} in {release_api}")


def _unzip(data: bytes, dest: Path, strip_root: bool) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            parts = Path(info.filename).parts
            if strip_root and len(parts) > 1:
                parts = parts[1:]
            if not parts or info.is_dir():
                continue
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(info))


def main() -> None:
    client_dir = HERE / "snapclient"
    if (client_dir / "snapclient.exe").exists():
        print("[ok] snapclient already present")
    else:
        if sys.platform != "win32":
            print("[skip] snapclient: non-Windows host — install snapclient via your package manager")
        else:
            url, tag = _asset(RELEASES, "win64")
            print(f"[..] downloading snapclient {tag} ...")
            _unzip(_get(url), client_dir, strip_root=False)
            print(f"[ok] snapclient -> {client_dir}")

    web_dir = HERE / "snapweb"
    if (web_dir / "index.html").exists():
        print("[ok] snapweb already present")
    else:
        url, tag = _asset(SNAPWEB_RELEASES, "snapweb.zip")
        print(f"[..] downloading snapweb {tag} ...")
        _unzip(_get(url), web_dir, strip_root=False)
        # some releases nest everything under a single folder
        if not (web_dir / "index.html").exists():
            inner = next((p for p in web_dir.iterdir() if (p / "index.html").exists()), None)
            if inner:
                for p in inner.iterdir():
                    p.rename(web_dir / p.name)
                inner.rmdir()
        print(f"[ok] snapweb -> {web_dir}")

    print("\nnext:  docker compose -f party/docker-compose.yml up -d")
    print("then:  party\\join-party-pc.bat   (lights this PC's star)")


if __name__ == "__main__":
    main()
