import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

import studio


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setitem(studio.state, "out_dir", str(tmp_path / "out"))
    monkeypatch.setitem(studio.state, "images", {})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), studio.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def call(base, path, body=None, raw=None, token=studio.TOKEN):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, headers={"X-Studio-Token": token})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def png_bytes(tmp_path, size=(24, 16), mode="RGBA"):
    p = tmp_path / "src.png"
    Image.new(mode, size, (10, 200, 90, 255)).save(p)
    return p.read_bytes()


def test_page_has_token(server):
    html = urllib.request.urlopen(server + "/").read().decode()
    assert studio.TOKEN in html and "__TOKEN__" not in html


def test_api_rejects_missing_token(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        call(server, "/api/status", token="wrong")
    assert e.value.code == 403


def test_upload_upscale_and_fetch_result(server, tmp_path):
    st = call(server, "/api/upload?name=My%20Design.png", raw=png_bytes(tmp_path))
    assert [i["name"] for i in st["images"]] == ["My Design.png"]
    call(server, "/api/upscale", {"scale": 2, "model": "photo", "fmt": "png", "dpi": True})
    for _ in range(200):
        st = call(server, "/api/status")
        if not st["running"]:
            break
        time.sleep(0.1)
    img = st["images"][0]
    assert img["status"] == "done", img["error"]
    assert img["output"] == "My Design_upscaled_2x.png"
    out = Image.open(Path(studio.state["out_dir"]) / img["output"])
    assert out.size == (48, 32)
    data = urllib.request.urlopen(f"{server}/img/result/{img['id']}?t={studio.TOKEN}").read()
    assert data[:4] == b"\x89PNG"


def test_batch_limit_and_bad_files(server, tmp_path):
    raw = png_bytes(tmp_path)
    for i in range(5):
        call(server, f"/api/upload?name=a{i}.png", raw=raw)
    with pytest.raises(urllib.error.HTTPError) as e:
        call(server, "/api/upload?name=a6.png", raw=raw)
    assert e.value.code == 400
    call(server, "/api/remove", {})
    with pytest.raises(urllib.error.HTTPError):
        call(server, "/api/upload?name=notes.txt", raw=b"hello")
    with pytest.raises(urllib.error.HTTPError):
        call(server, "/api/upload?name=fake.png", raw=b"not an image")
    assert call(server, "/api/status")["images"] == []


def test_image_route_needs_token(server, tmp_path):
    st = call(server, "/api/upload?name=a.png", raw=png_bytes(tmp_path))
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"{server}/img/original/{st['images'][0]['id']}?t=nope")
    assert e.value.code == 404
