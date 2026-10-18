"""The stdlib frame relay (arms/sim/supermarket/demo/frame_stream.py) on an ephemeral port against a temp run dir."""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
from pathlib import Path

import pytest

from arms.sim.supermarket.demo.frame_stream import FrameStore, make_server, parse_range

JPEG_A = b"\xff\xd8" + b"A" * 200 + b"\xff\xd9"
JPEG_B = b"\xff\xd8" + b"B" * 300 + b"\xff\xd9"
MP4 = bytes(range(256)) * 4  # 1024 bytes, enough to check byte ranges


@pytest.fixture
def server(tmp_path: Path):
    runs = tmp_path / "runs"
    old = runs / "old" / "frames" / "director"
    new = runs / "new" / "frames" / "director"
    for d in (old, new):
        d.mkdir(parents=True)
    (old / "000001.jpg").write_bytes(JPEG_A)
    (new / "000001.jpg").write_bytes(JPEG_A)
    (new / "000002.jpg").write_bytes(JPEG_B)
    (new / "000003.jpg").write_bytes(b"\xff\xd8half-written")  # too new and truncated: never served
    past = time.time() - 5
    os.utime(old / "000001.jpg", (past - 60, past - 60))
    os.utime(old, (past - 60, past - 60))
    for f in ("000001.jpg", "000002.jpg"):
        os.utime(new / f, (past, past))
    videos = tmp_path / "videos" / "nested"
    videos.mkdir(parents=True)
    (videos / "trip.mp4").write_bytes(MP4)

    srv = make_server(FrameStore(runs, None, [tmp_path / "videos"]), "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _get(port: int, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body


def test_cams_lists_newest_run(server: int) -> None:
    status, headers, body = _get(server, "/cams")
    assert status == 200
    assert headers["access-control-allow-origin"] == "*"
    cams = json.loads(body)
    assert cams["run"] == "new"
    assert cams["cameras"] == [{"name": "director", "frames": 3, "latest": "000003.jpg"}]
    assert json.loads(_get(server, "/health")[2])["ok"] is True


def test_frame_skips_partial_file(server: int) -> None:
    status, headers, body = _get(server, "/frame/director.jpg")
    assert status == 200
    assert headers["content-type"] == "image/jpeg"
    assert body == JPEG_B
    assert _get(server, "/frame/nope.jpg")[0] == 404


def test_stream_is_multipart_mjpeg(server: int) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
    conn.request("GET", "/stream/director.mjpg")
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "multipart/x-mixed-replace; boundary=pickyframe"
    chunk = resp.read(200 + len(JPEG_B))
    conn.close()
    assert chunk.startswith(b"--pickyframe\r\n")
    assert b"Content-Type: image/jpeg" in chunk
    assert JPEG_B in chunk
    assert _get(server, "/stream/nope.mjpg")[0] == 404


def test_video_range_requests(server: int) -> None:
    rows = json.loads(_get(server, "/videos")[2])
    assert [r["name"] for r in rows] == ["trip.mp4"]
    status, headers, body = _get(server, "/video/trip.mp4")
    assert status == 200 and body == MP4 and headers["accept-ranges"] == "bytes"
    status, headers, body = _get(server, "/video/trip.mp4", {"Range": "bytes=100-199"})
    assert status == 206
    assert headers["content-range"] == "bytes 100-199/1024"
    assert body == MP4[100:200]
    status, _, body = _get(server, "/video/trip.mp4", {"Range": "bytes=-24"})
    assert status == 206 and body == MP4[-24:]
    assert _get(server, "/video/trip.mp4", {"Range": "bytes=5000-"})[0] == 416


def test_parse_range() -> None:
    assert parse_range(None, 10) is None
    assert parse_range("bytes=0-", 10) == (0, 9)
    assert parse_range("bytes=2-100", 10) == (2, 9)
    assert parse_range("bytes=-3", 10) == (7, 9)
    assert parse_range("bytes=20-", 10) is False
