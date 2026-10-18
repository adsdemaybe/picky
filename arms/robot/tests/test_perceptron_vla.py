"""Perceptron robotics VLA client + policy backends, against an in-process fake of robotics.perceptron.inc (httpx
MockTransport; no network, no key)."""

import hashlib
import io
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from arms.robot import perceptron_vla as pv
from arms.robot import policy_backends as pb

KEY = "team-key-0123456789abcdef"
APP_TOKEN = "app-session-token-xyz"
MODAL_TOKEN = "modal-session-token-xyz"
BASE = "https://robotics.test"
GPU = "https://gpu.test"
CONTRACT_ID = "c" * 64


async def nosleep(_: float) -> None:
    return None


def parse_multipart(req: httpx.Request) -> dict[str, bytes]:
    boundary = req.headers["content-type"].split("boundary=")[1].encode()
    out: dict[str, bytes] = {}
    for part in req.content.split(b"--" + boundary)[1:-1]:
        head, _, data = part.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]+)"', head)
        assert name is not None
        out[name.group(1).decode()] = data[:-2]
    return out


def jpeg(size: tuple[int, int] = (640, 480)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (120, 130, 140)).save(buf, format="JPEG")
    return buf.getvalue()


class FakeRobotics:
    """Stateful fake of the control plane + one inference endpoint. Records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.datasets: dict[str, dict[str, Any]] = {}
        self.idem: dict[str, tuple[str, str]] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.received: dict[str, bytes] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.job_polls = 0
        self.dataset_polls = 0
        self.grants: list[str] = []
        self.sessions: dict[str, dict[str, Any]] = {}
        self.predicts: list[dict[str, Any]] = []
        self.chunk_rows: list[list[float]] | None = None
        self.overrides: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response | None]] = {}
        self.logs = [{"level": "info", "message": f"step {i}"} for i in range(5)]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- helpers
    def _auth_ok(self, req: httpx.Request) -> bool:
        return req.headers.get("authorization") == f"Bearer {KEY}"

    @staticmethod
    def err(status: int, code: str, message: str = "", headers: dict[str, str] | None = None) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": message}}, headers=headers)

    def _idem(self, req: httpx.Request, kind: str, make: Callable[[], str]) -> tuple[str, bool] | httpx.Response:
        key = req.headers.get("idempotency-key")
        if not key:
            return self.err(422, "idempotency_key_required")
        body = req.content.decode()
        if key in self.idem:
            prev_body, rid = self.idem[key]
            if prev_body != body:
                return self.err(409, "idempotency_conflict", "same key, different body")
            return rid, True
        rid = make()
        self.idem[key] = (body, rid)
        return rid, False

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path, method = req.url.path, req.method
        for (m, pat), fn in list(self.overrides.items()):
            if m == method and re.fullmatch(pat, path):
                resp = fn(req)
                if resp is not None:
                    return resp
        if req.url.host == "gpu.test":
            return self.inference(req)
        if path.startswith("/v1/uploads/"):
            return self.put_upload(req)
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if path.startswith("/v1/operations/"):
            return httpx.Response(200, json={"id": "job_polled", "status": "queued"})
        if not self._auth_ok(req):
            return self.err(401, "unauthorized", "invalid API key")
        if path == "/v1/capabilities":
            return httpx.Response(
                200, json={"modes": ["expert_only", "backbone_lora", "full"], "action_horizons": [20, 30]}
            )
        if path == "/v1/datasets" and method == "POST":
            body = json.loads(req.content)
            got = self._idem(req, "dataset", lambda: f"ds_{len(self.datasets) + 1}")
            if isinstance(got, httpx.Response):
                return got
            rid, replay = got
            if not replay:
                status = "uploading" if body.get("source_kind") == "upload" else "ingesting"
                self.datasets[rid] = {"id": rid, "status": status, **body}
            return httpx.Response(202, json=self.datasets[rid])
        m = re.fullmatch(r"/v1/datasets/([\w-]+)(/\w+)?", path)
        if m:
            ds = self.datasets.get(m.group(1))
            if ds is None:
                return self.err(404, "not_found")
            sub = m.group(2)
            if sub == "/uploads":
                if ds["status"] != "uploading":
                    return self.err(409, "invalid_state", "dataset already finalized")
                files = json.loads(req.content)["files"]
                out = []
                for f in files:
                    grant = f"g{len(self.uploads) + 1}"
                    self.uploads[grant] = {**f, "dataset": ds["id"]}
                    out.append(
                        {
                            "path": f["path"],
                            "method": "PUT",
                            "url": f"{BASE}/v1/uploads/{grant}?sig=abc",
                            "headers": {"Content-Length": str(f["size_bytes"]), "x-upload-checksum": f["sha256"]},
                            "expires_at": "2026-10-17T23:00:00Z",
                        }
                    )
                return httpx.Response(200, json={"files": out})
            if sub == "/finalize":
                if ds["status"] != "uploading":
                    return self.err(409, "invalid_state")
                ds["status"] = "validating"
                return httpx.Response(202, json={"id": ds["id"], "status": "validating"})
            if sub == "/validation":
                return httpx.Response(200, json={"dataset_id": ds["id"], "status": "passed", "errors": []})
            self.dataset_polls += 1
            if ds["status"] in ("validating", "ingesting") and self.dataset_polls >= 2:
                ds["status"] = "ready"
            return httpx.Response(200, json=ds)
        if path == "/v1/jobs" and method == "POST":
            got = self._idem(req, "job", lambda: f"job_{len(self.jobs) + 1}")
            if isinstance(got, httpx.Response):
                return got
            rid, replay = got
            if not replay:
                self.jobs[rid] = {"id": rid, "status": "queued", **json.loads(req.content)}
            return httpx.Response(202, json=self.jobs[rid])
        if path == "/v1/jobs":
            return httpx.Response(200, json={"items": list(self.jobs.values())})
        m = re.fullmatch(r"/v1/jobs/([\w-]+)(/\w+)?", path)
        if m:
            job = self.jobs.get(m.group(1))
            if job is None:
                return self.err(404, "not_found")
            sub = m.group(2)
            cursor = int(req.url.params.get("cursor", "0"))
            if sub == "/logs":
                events = self.logs[cursor : cursor + 2]
                return httpx.Response(
                    200, json={"events": events, "next_cursor": cursor + len(events), "truncated_before": None}
                )
            if sub == "/metrics":
                pts = [{"step": 10 * (cursor + 1), "loss": 1.0 / (cursor + 1)}]
                return httpx.Response(200, json={"metrics": pts, "next_cursor": cursor + 1})
            if sub == "/cancel":
                job["status"] = "cancelled"
                return httpx.Response(200, json=job)
            self.job_polls += 1
            if self.job_polls == 1:
                job["status"] = "running"
            elif self.job_polls >= 2:
                job.update(status="succeeded", checkpoint_id="ckpt_1")
            return httpx.Response(200, json=job)
        if path == "/v1/checkpoints/ckpt_1":
            return httpx.Response(200, json={"id": "ckpt_1", "status": "ready", "job_id": "job_1"})
        if path == "/v1/deployments":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": "dep_qual", "status": "ready", "checkpoint_id": "ckpt_1", "target": "private_cluster"},
                        {"id": "dep_1", "status": "ready", "checkpoint_id": "ckpt_1", "target": "modal"},
                        {"id": "dep_other", "status": "ready", "checkpoint_id": "ckpt_9", "target": "modal"},
                    ]
                },
            )
        m = re.fullmatch(r"/v1/deployments/([\w-]+)(/\w+)?", path)
        if m:
            sub = m.group(2)
            if sub == "/contract":
                return httpx.Response(
                    200,
                    json={
                        "contract_id": CONTRACT_ID,
                        "contract": {
                            "robot": "so100",
                            "cameras": {
                                "observation.images.side": {"shape": [480, 640, 3]},
                                "observation.images.wrist": {"shape": [480, 640, 3]},
                            },
                            "state": {"names": list(pv.STATE_NAMES), "units": {"arm": "degrees", "gripper": "0-100"}},
                            "timing": {"fps": 30, "action_horizon": 30, "execution_horizon": 10},
                        },
                    },
                )
            if sub == "/sessions" and method == "POST":
                body = json.loads(req.content)
                self.grants.append(body["session_id"])
                return httpx.Response(
                    201,
                    json={
                        "session_id": body["session_id"],
                        "endpoint_url": GPU,
                        "application_session_token": APP_TOKEN,
                        "modal_session_token": MODAL_TOKEN,
                        "expires_at": "2026-10-18T05:00:00Z",
                    },
                )
            return httpx.Response(200, json={"id": m.group(1), "status": "ready", "checkpoint_id": "ckpt_1"})
        return self.err(404, "not_found", path)

    def put_upload(self, req: httpx.Request) -> httpx.Response:
        assert "authorization" not in req.headers, "team key sent to a signed upload URL"
        grant = req.url.path.rsplit("/", 1)[1]
        if req.url.params.get("sig") != "abc" or grant not in self.uploads:
            return self.err(403, "bad_signature")
        meta = self.uploads[grant]
        assert int(req.headers["content-length"]) == meta["size_bytes"]
        assert req.headers["x-upload-checksum"] == meta["sha256"]
        assert hashlib.sha256(req.content).hexdigest() == meta["sha256"]
        self.received[meta["path"]] = req.content
        return httpx.Response(200)

    def inference(self, req: httpx.Request) -> httpx.Response:
        assert req.headers.get("authorization") == f"Bearer {APP_TOKEN}"
        assert req.headers.get("modal-authorization") == f"Bearer {MODAL_TOKEN}"
        path = req.url.path
        m = re.fullmatch(r"/v1/inference/deployments/([\w-]+)/sessions", path)
        if m:
            body = json.loads(req.content)
            assert body["contract_id"] == CONTRACT_ID
            sid = self.grants[-1]
            self.sessions[sid] = {"instruction": body["instruction"], "rtc": body["rtc_enabled"], "seq": -1}
            return httpx.Response(
                200, json={"session_id": sid, "deployment_id": m.group(1), "contract_id": CONTRACT_ID, **body}
            )
        m = re.fullmatch(r"/v1/inference/sessions/([\w-]+)/(predict|reset)", path)
        if m:
            sid, op = m.group(1), m.group(2)
            if sid not in self.sessions:
                return self.err(404, "session_not_found", "expired")
            if op == "reset":
                self.sessions[sid]["seq"] = -1
                return httpx.Response(200, json={"session_id": sid, "reset": True})
            parts = parse_multipart(req)
            meta = json.loads(parts["metadata"])
            assert parts["side"][:2] == b"\xff\xd8" and parts["wrist"][:2] == b"\xff\xd8"
            assert meta["sequence"] > self.sessions[sid]["seq"]
            self.sessions[sid]["seq"] = meta["sequence"]
            self.predicts.append(meta)
            n = len(self.predicts)
            rows = self.chunk_rows or [[float(i), -90.0, 80.0, 40.0, 0.0, 50.0] for i in range(30)]
            out: dict[str, Any] = {
                "chunk_id": f"chunk-{n}",
                "sequence": meta["sequence"],
                "actions": rows,
                "execution_prefix": 10,
                "timings": {"infer_ms": 42.0},
            }
            if meta.get("rtc"):
                out["execution_prefix"] = {"start_index": meta["rtc"]["delay"], "length": 10}
            return httpx.Response(200, json=out)
        m = re.fullmatch(r"/v1/inference/sessions/([\w-]+)", path)
        if m and req.method == "DELETE":
            self.sessions.pop(m.group(1), None)
            return httpx.Response(200, json={"session_id": m.group(1), "deleted": True})
        if path == "/v1/inference/health":
            return httpx.Response(200, json={"status": "ok"})
        return self.err(404, "not_found", path)

    def team_key_leaks(self) -> list[str]:
        """Requests that carried the team key anywhere but the control plane's /v1 routes."""
        bad = []
        for r in self.requests:
            carries = KEY in " ".join(r.headers.values())
            control = r.url.host == "robotics.test" and not r.url.path.startswith("/v1/uploads/")
            if carries and not control:
                bad.append(str(r.url))
        return bad


@pytest.fixture
def fake() -> FakeRobotics:
    return FakeRobotics()


@pytest.fixture(autouse=True)
def _state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(pv, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.delenv(pv.DEPLOYMENT_ENV, raising=False)


def client(fake: FakeRobotics, **kw: Any) -> pv.RoboticsClient:
    return pv.RoboticsClient(KEY, base_url=BASE, transport=fake.transport, sleep=nosleep, **kw)


def make_dataset(root: Path, *, fps: int = 30, episodes: int = 3, wrist: bool = True) -> Path:
    feats: dict[str, Any] = {
        "observation.images.side": {"dtype": "video", "shape": [480, 640, 3], "names": ["height", "width", "channels"]},
        "observation.state": {"dtype": "float32", "shape": [6], "names": list(pv.STATE_NAMES)},
        "action": {"dtype": "float32", "shape": [6], "names": list(pv.STATE_NAMES)},
    }
    if wrist:
        feats["observation.images.wrist"] = dict(feats["observation.images.side"])
    info = {
        "codebase_version": "v2.1",
        "robot_type": "so101_follower",
        "fps": fps,
        "total_episodes": episodes,
        "total_frames": 300,
        "splits": {"train": "0:2", "validation": "2:3"},
        "features": feats,
    }
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").write_text(json.dumps(info))
    (root / "meta" / "episodes.jsonl").write_text('{"episode_index": 0}\n')
    (root / "data" / "chunk-000").mkdir(parents=True)
    (root / "data" / "chunk-000" / "episode_000000.parquet").write_bytes(b"PAR1" + b"\x00" * 64)
    for cam in ("side", "wrist"):
        d = root / "videos" / "chunk-000" / f"observation.images.{cam}"
        d.mkdir(parents=True)
        (d / "episode_000000.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + cam.encode())
    (root / "README.md").write_text("not uploaded")
    (root / ".cache").mkdir()
    (root / ".cache" / "junk.json").write_text("{}")
    return root


# ---- end to end ------------------------------------------------------------------------------------------------------
async def test_pipeline_upload_end_to_end(fake, tmp_path):
    root = make_dataset(tmp_path / "picky_shelf_picks")
    lines: list[str] = []
    async with client(fake) as c:
        res = await pv.run_pipeline(
            c, str(root), job={"steps": 200, "action_horizon": 30}, instruction="pick the salsa", progress=lines.append
        )
    assert res.dataset_id == "ds_1" and res.job_id == "job_1" and res.deployment_id == "dep_1"
    assert res.contract_id == CONTRACT_ID and res.chunk_rows == 30
    # every uploadable file arrived byte-exact (hidden + unsupported files skipped)
    assert sorted(fake.received) == [
        "data/chunk-000/episode_000000.parquet",
        "meta/episodes.jsonl",
        "meta/info.json",
        "videos/chunk-000/observation.images.side/episode_000000.mp4",
        "videos/chunk-000/observation.images.wrist/episode_000000.mp4",
    ]
    posts = [r for r in fake.requests if r.method == "POST" and r.url.path in ("/v1/datasets", "/v1/jobs")]
    assert all(r.headers.get("idempotency-key", "").startswith("picky-") for r in posts)
    job_body = json.loads(next(r for r in posts if r.url.path == "/v1/jobs").content)
    assert job_body == {
        "dataset_id": "ds_1",
        "backend": "standard",
        "mode": "expert_only",
        "action_horizon": 30,
        "rtc_enabled": True,
        "steps": 200,
    }
    # one predict: rest pose, gripper -10 USD deg -> 0 on the 0-100 scale; session deleted afterwards
    meta = fake.predicts[0]
    assert meta["contract_id"] == CONTRACT_ID and meta["sequence"] == 0 and meta["image_encoding"] == "jpeg"
    assert meta["state"] == pytest.approx([0.0, -100.0, 90.0, 50.0, 0.0, 0.0])
    assert fake.sessions == {}
    # first row back in USD degrees: gripper 50 -> 45 deg
    assert res.first_action == pytest.approx([0.0, -90.0, 80.0, 40.0, 0.0, 45.0])
    assert fake.team_key_leaks() == []
    assert any("PIPELINE" not in x and "predict ok" in x for x in lines)
    assert json.loads(pv.STATE_FILE.read_text())["deployment_id"] == "dep_1"
    assert pv.default_deployment_id() == "dep_1"


async def test_rerun_resumes_same_dataset_and_job(fake, tmp_path):
    root = make_dataset(tmp_path / "ds")
    async with client(fake) as c:
        first = await pv.run_pipeline(c, str(root), progress=lambda _: None)
        second = await pv.run_pipeline(c, str(root), progress=lambda _: None)
    assert (first.dataset_id, first.job_id) == (second.dataset_id, second.job_id)
    assert len(fake.datasets) == 1 and len(fake.jobs) == 1  # replayed via the idempotency keys, 409 on reserve skipped


async def test_hf_dataset_flow(fake):
    async with client(fake) as c:
        ds = await c.create_dataset_hf("lerobot/svla_so100_stacking")
        assert ds.id == "ds_1" and ds.phase == "ingesting"
        body = json.loads(fake.requests[-1].content)
        assert body == {
            "name": "lerobot-svla_so100_stacking",
            "source_kind": "huggingface",
            "repo_id": "lerobot/svla_so100_stacking",
            "revision": "main",
        }
        ds, val = await c.wait_dataset(ds.id)
        assert ds.phase == "ready" and val is not None and val.ok is True


# ---- protocol details ------------------------------------------------------------------------------------------------
async def test_error_envelope_and_rejected_key_hint(fake):
    async with pv.RoboticsClient("mk15-vision-key-not-a-team-key", base_url=BASE, transport=fake.transport) as c:
        with pytest.raises(pv.RoboticsAPIError) as ei:
            await c.capabilities()
    err = ei.value
    assert err.status == 401 and err.code == "unauthorized" and err.message == "invalid API key"
    assert "PERCEPTRON_API_KEY does not work here" in str(err)
    assert "mk15-vision-key" not in str(err)


async def test_idempotency_conflict_is_409(fake):
    async with client(fake) as c:
        await c.submit_job(pv.JobRequest(dataset_id="ds_1"), idem_key="k1")
        with pytest.raises(pv.RoboticsAPIError) as ei:
            await c.submit_job(pv.JobRequest(dataset_id="ds_1", steps=5), idem_key="k1")
    assert ei.value.status == 409 and ei.value.code == "idempotency_conflict"


def test_idempotency_key_is_stable_unless_fresh():
    body = {"dataset_id": "ds_1", "steps": 10}
    assert pv.idempotency_key("train", body) == pv.idempotency_key("train", dict(reversed(body.items())))
    assert pv.idempotency_key("train", body) != pv.idempotency_key("train", {**body, "steps": 11})
    assert pv.idempotency_key("train", body, fresh=True) != pv.idempotency_key("train", body, fresh=True)


async def test_429_retry_after_then_success(fake):
    calls = {"n": 0}
    slept: list[float] = []

    def limited(_: httpx.Request) -> httpx.Response | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return fake.err(429, "rate_limited", headers={"Retry-After": "7"})
        return None

    async def sleep(s: float) -> None:
        slept.append(s)

    fake.overrides[("GET", "/v1/deployments")] = limited
    async with pv.RoboticsClient(KEY, base_url=BASE, transport=fake.transport, sleep=sleep) as c:
        deps = await c.list_deployments()
    assert [d.id for d in deps] == ["dep_qual", "dep_1", "dep_other"] and slept == [7.0]


async def test_5xx_retried_for_get_not_for_grant(fake):
    n = {"get": 0, "grant": 0}

    def flaky_get(_: httpx.Request) -> httpx.Response | None:
        n["get"] += 1
        return fake.err(503, "unavailable") if n["get"] < 3 else None

    def flaky_grant(_: httpx.Request) -> httpx.Response | None:
        n["grant"] += 1
        return fake.err(503, "unavailable")

    fake.overrides[("GET", r"/v1/deployments/dep_1")] = flaky_get
    fake.overrides[("POST", r"/v1/deployments/dep_1/sessions")] = flaky_grant
    async with client(fake) as c:
        assert (await c.get_deployment("dep_1")).phase == "ready"
        with pytest.raises(pv.RoboticsAPIError):
            await c.grant_session("dep_1", session_id="robot-01")
    assert n == {"get": 3, "grant": 1}


async def test_303_is_polled_same_origin_without_credentials(fake):
    polls = {"n": 0}

    def redirect(_: httpx.Request) -> httpx.Response:
        return httpx.Response(303, headers={"Location": "/v1/operations/op1?sig=xyz"})

    def op(req: httpx.Request) -> httpx.Response:
        assert "authorization" not in req.headers and req.url.params["sig"] == "xyz"
        polls["n"] += 1
        if polls["n"] == 1:
            return httpx.Response(202, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"id": "job_polled", "status": "queued"})

    fake.overrides[("POST", "/v1/jobs")] = redirect
    fake.overrides[("GET", r"/v1/operations/op1")] = op
    async with client(fake) as c:
        job = await c.submit_job(pv.JobRequest(dataset_id="ds_1"))
    assert job.id == "job_polled" and polls["n"] == 2
    assert fake.team_key_leaks() == []


async def test_303_cross_origin_refused(fake):
    fake.overrides[("POST", "/v1/jobs")] = lambda _: httpx.Response(303, headers={"Location": "https://evil.test/x"})
    async with client(fake) as c:
        with pytest.raises(pv.RoboticsAPIError) as ei:
            await c.submit_job(pv.JobRequest(dataset_id="ds_1"))
    assert ei.value.code == "unsafe_redirect"
    assert not any(r.url.host == "evil.test" for r in fake.requests)


async def test_json_body_cap_is_checked_locally(fake):
    # a full valid upload batch (500 x 512-char paths) stays under the cap ...
    files = [pv.UploadFile(path=("a/" * 250) + f"{i:03d}.json", size_bytes=1, sha256="0" * 64) for i in range(500)]
    assert len(pv.UploadRequest(files=files).model_dump_json()) < pv.MAX_JSON_BODY_BYTES
    # ... and anything bigger is refused before it is sent
    async with client(fake) as c:
        with pytest.raises(pv.RoboticsConfigError, match="512 KiB"):
            await c._request("POST", "/v1/jobs", json_body={"blob": "x" * pv.MAX_JSON_BODY_BYTES})
    assert not any(r.url.path == "/v1/jobs" for r in fake.requests)
    with pytest.raises(ValueError):
        pv.UploadRequest(files=files + files[:1])  # > 500 files per batch


async def test_logs_and_metrics_cursors(fake):
    async with client(fake) as c:
        job = await c.submit_job(pv.JobRequest(dataset_id="ds_1"))
        events = [ev async for ev, _ in c.iter_logs(job.id)]
        page = await c.job_metrics(job.id, cursor=3)
    assert [e["message"] for e in events] == [f"step {i}" for i in range(5)]
    cursors = [int(r.url.params["cursor"]) for r in fake.requests if r.url.path.endswith("/logs")]
    assert cursors == [0, 2, 4, 5]
    assert page.next_cursor == 4 and page.metrics[0]["step"] == 40


def test_request_models_follow_the_schema():
    with pytest.raises(ValueError):
        pv.JobRequest(dataset_id="ds_1", backend="shared", mode="full")
    with pytest.raises(ValueError):
        pv.JobRequest(dataset_id="ds_1", steps=10_001)
    with pytest.raises(ValueError):
        pv.JobRequest(dataset_id="ds_1", action_horizon=25)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        pv.DatasetRequest(name="x", source_kind="upload", repo_id="a/b")
    with pytest.raises(ValueError):
        pv.SessionGrantRequest(session_id="robot-01", ttl_seconds=21_601)
    with pytest.raises(ValueError):
        pv.RtcHint(prior_chunk_id="c", prior_start_index=0, delay=2, committed_actions=[[0.0] * 6])
    assert pv.DatasetRequest(name="x", source_kind="upload").body() == {"name": "x", "source_kind": "upload"}


def test_inference_base_handles_both_doc_forms():
    assert pv.inference_base("https://gpu.test") == "https://gpu.test/v1/inference"
    assert pv.inference_base("https://gpu.test/") == "https://gpu.test/v1/inference"
    assert pv.inference_base("https://gpu.test/v1") == "https://gpu.test/v1/inference"
    assert pv.inference_base("https://gpu.test/v1/inference/") == "https://gpu.test/v1/inference"


async def test_key_guard_refuses_team_key_on_inference(fake):
    async with client(fake) as c:
        contract = await c.get_contract("dep_1")
        grant = pv.SessionGrant(session_id="s1", endpoint_url=GPU, application_session_token=KEY)
        session = pv.InferenceSession(
            grant, "dep_1", contract=contract, transport=fake.transport, forbidden_fingerprints=[c.key_fingerprint]
        )
        with pytest.raises(pv.RoboticsConfigError, match="refusing to send the team API key"):
            await session.create("pick")
        await session.aclose()
    assert not any(r.url.host == "gpu.test" for r in fake.requests)


def test_session_grant_never_shows_tokens():
    g = pv.SessionGrant(
        session_id="s", endpoint_url=GPU, application_session_token=APP_TOKEN, modal_session_token=MODAL_TOKEN
    )
    assert APP_TOKEN not in repr(g) and MODAL_TOKEN not in json.dumps(g.public())


def test_contract_accessors_and_defaults():
    c = pv.ServingContract.model_validate(
        {
            "contract_id": CONTRACT_ID,
            "contract": {
                "observation": {"images": {"observation.images.side": {"shape": [3, 480, 640]}}},
                "timing": {"control_hz": 30, "horizon": 20},
            },
        }
    )
    assert c.cameras == {"side": (640, 480)} and c.action_horizon == 20 and c.action_hz == 30.0
    assert c.state_names == list(pv.STATE_NAMES) and c.arm_units == "degrees" and c.gripper_range == (0.0, 100.0)
    flat = pv.ServingContract.model_validate({"id": CONTRACT_ID, "units": "radians"})
    assert flat.contract_id == CONTRACT_ID and flat.arm_units == "radians"


def test_predict_result_parsing_variants():
    r = pv.PredictResult.from_api(
        {"chunk": {"chunk_id": "c9", "sequence": 3, "actions": [[1, 2, 3, 4, 5, 6]]}, "execution_prefix": [[0] * 6]}
    )
    assert r.chunk_id == "c9" and r.sequence == 3 and r.execute_count == 1 and r.actions == [[1.0, 2, 3, 4, 5, 6]]
    with pytest.raises(pv.RoboticsAPIError):
        pv.PredictResult.from_api({"chunk_id": "x"})


# ---- local dataset check ---------------------------------------------------------------------------------------------
def test_dataset_check_ok_and_failures(tmp_path):
    assert pv.check_dataset_dir(make_dataset(tmp_path / "ok")).ok
    bad = pv.check_dataset_dir(make_dataset(tmp_path / "bad", fps=15, episodes=1, wrist=False))
    text = " | ".join(bad.errors)
    assert "fps 15" in text and "observation.images.wrist missing" in text and "total_episodes 1" in text
    info = json.loads((tmp_path / "ok" / "meta" / "info.json").read_text())
    info["features"]["action"]["names"] = list(reversed(pv.STATE_NAMES))
    (tmp_path / "ok" / "meta" / "info.json").write_text(json.dumps(info))
    assert any("action names" in e for e in pv.check_dataset_dir(tmp_path / "ok").errors)
    assert not pv.check_dataset_dir(tmp_path / "missing").ok


def test_scan_skips_hidden_unsupported_and_empty(tmp_path):
    root = make_dataset(tmp_path / "d")
    (root / "meta" / "empty.json").write_text("")
    files, skipped = pv.scan_dataset_dir(root)
    paths = {f.path for f in files}
    assert "README.md" not in paths and ".cache/junk.json" not in paths and "meta/empty.json" not in paths
    assert any("README.md" in s for s in skipped) and any("empty" in s for s in skipped)
    info = next(f for f in files if f.path == "meta/info.json")
    assert info.sha256 == hashlib.sha256((root / "meta" / "info.json").read_bytes()).hexdigest()


def test_cli_dataset_check_and_missing_key(tmp_path, monkeypatch, capsys):
    root = make_dataset(tmp_path / "d")
    assert pv.main(["dataset-check", str(root)]) == 0
    assert "dataset check: OK" in capsys.readouterr().out
    monkeypatch.setattr(pv, "resolve_api_key", lambda: ("", ""))
    assert pv.main(["capabilities"]) == 2
    assert pv.KEY_NAME in capsys.readouterr().err


def test_cli_train_with_fake_service(fake, monkeypatch, capsys):
    real = pv.RoboticsClient

    def patched(api_key=None, **kw):  # noqa: ANN001, ANN202
        return real(KEY, transport=fake.transport, sleep=nosleep, **{**kw, "base_url": BASE})

    monkeypatch.setattr(pv, "RoboticsClient", patched)
    assert pv.main(["train", "ds_1", "--steps", "50", "--horizon", "20", "--wait"]) == 0
    out = capsys.readouterr().out
    assert "job job_1: succeeded" in out and "checkpoint=ckpt_1" in out and "log: info step 0" in out
    body = json.loads(next(r for r in fake.requests if r.url.path == "/v1/jobs").content)
    assert body["steps"] == 50 and body["action_horizon"] == 20
    assert KEY not in out


# ---- policy backends -------------------------------------------------------------------------------------------------
def test_unit_map_round_trip_and_gripper_scale():
    um = pb.So101UnitMap()
    usd = [10.0, -50.0, 30.0, 20.0, -90.0, 100.0]
    pol = um.to_policy(usd)
    assert pol[:5] == usd[:5] and pol[5] == pytest.approx(100.0)
    assert um.to_policy([0, 0, 0, 0, 0, -10])[5] == pytest.approx(0.0)
    assert um.from_policy(pol) == pytest.approx(usd)
    rad = pb.So101UnitMap.from_contract(pv.ServingContract(contract_id="x", contract={"units": "radians"}))
    assert rad.to_policy([180.0, 0, 0, 0, 0, -10])[0] == pytest.approx(3.14159, rel=1e-4)
    with pytest.raises(ValueError):
        um.to_policy([0.0] * 5)


def test_check_rows_clips_and_flags_divergence():
    rows, clipped = pb.check_rows([[0, -105, 0, 0, 0, 0], [0, 0, 0, 0, 0, 101]])
    assert clipped == 2 and rows[0][1] == -100.0 and rows[1][5] == 100.0
    for bad in ([], [[0.0] * 5], [[float("nan")] + [0.0] * 5], [[400.0] + [0.0] * 5]):
        with pytest.raises(pb.PolicyBackendError) as ei:
            pb.check_rows(bad)
        assert ei.value.code == "policy_diverged"


async def test_perceptron_backend_rtc_and_units(fake):
    async with client(fake) as c:
        backend = pb.PerceptronBackend("dep_1", client=c)
        await backend.reset("pick the salsa")
        assert backend.info.action_hz == 30.0 and backend.info.default_execute == 10 and backend.info.supports_rtc
        first = await backend.predict(pb.REST_POSE_USD_DEG, jpeg(), jpeg((320, 240)), frames_blurred=True)
        assert first.chunk_id == "chunk-1" and first.execute_from == 0 and first.execute_count == 10
        assert not first.rtc_used and len(first.rows_to_execute()) == 10
        second = await backend.predict(first.actions_deg[12], jpeg(), jpeg(), frames_blurred=True, executed=12, delay=3)
        rtc = fake.predicts[1]["rtc"]
        assert rtc["prior_chunk_id"] == "chunk-1" and rtc["prior_start_index"] == 12 and rtc["delay"] == 3
        assert rtc["committed_actions"] == first.policy_actions[12:15]  # contract units, not USD degrees
        assert second.rtc_used and second.execute_from == 3 and fake.predicts[1]["sequence"] == 1
        # same instruction -> server-side reset (no new grant); new instruction -> new session
        await backend.reset("pick the salsa")
        assert len(fake.grants) == 1 and fake.predicts[-1]["sequence"] == 1
        await backend.reset("pick the chips")
        assert len(fake.grants) == 2 and len(fake.sessions) == 1
        await backend.close()
    assert fake.sessions == {} and fake.team_key_leaks() == []


async def test_perceptron_backend_reopens_expired_session(fake):
    async with client(fake) as c:
        backend = pb.PerceptronBackend("dep_1", client=c)
        await backend.reset("pick")
        fake.sessions.clear()  # expired after inactivity
        chunk = await backend.predict(pb.REST_POSE_USD_DEG, jpeg(), jpeg(), frames_blurred=True, executed=5, delay=2)
        assert chunk.chunk_id and not chunk.rtc_used and len(fake.grants) == 2
        await backend.close()


async def test_perceptron_backend_errors(fake):
    async with client(fake) as c:
        backend = pb.PerceptronBackend("dep_1", client=c)
        with pytest.raises(pb.PolicyBackendError) as ei:
            await backend.predict(pb.REST_POSE_USD_DEG, jpeg(), jpeg(), frames_blurred=True)
        assert ei.value.code == "policy_unavailable"  # no reset yet
        await backend.reset("pick")
        with pytest.raises(ValueError, match="blurred"):
            await backend.predict(pb.REST_POSE_USD_DEG, jpeg(), jpeg(), frames_blurred=False)
        fake.chunk_rows = [[999.0, 0, 0, 0, 0, 0]]
        with pytest.raises(pb.PolicyBackendError) as ei:
            await backend.predict(pb.REST_POSE_USD_DEG, jpeg(), jpeg(), frames_blurred=True)
        assert ei.value.code == "policy_diverged"

        def slow(_: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        fake.overrides[("POST", r"/v1/inference/sessions/.*/predict")] = slow
        with pytest.raises(pb.PolicyBackendError) as ei:
            await backend.predict(pb.REST_POSE_USD_DEG, jpeg(), jpeg(), frames_blurred=True)
        assert ei.value.code == "policy_timeout"
        await backend.close()
    no_dep = pb.PerceptronBackend(None, client=pv.RoboticsClient(KEY, base_url=BASE, transport=fake.transport))
    with pytest.raises(pb.PolicyBackendError) as ei:
        await no_dep.reset("pick")
    assert ei.value.code == "policy_unavailable" and pv.DEPLOYMENT_ENV in ei.value.message


def smolvla_transport(seen: list[dict[str, Any]], status: str = "ok", infer_status: int = 200) -> httpx.MockTransport:
    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/health":
            return httpx.Response(200, json={"status": status})
        if req.url.path == "/infer":
            body = json.loads(req.content)
            seen.append(body)
            if infer_status != 200:
                return httpx.Response(infer_status, json={"error": "model not loaded"})
            rows = [[0.0, -100.0 + i, 90.0, 50.0, 0.0, -10.0 + i] for i in range(50)]
            return httpx.Response(
                200,
                json={"actions_deg": rows, "model": "edge-inference/smolvla@71cf", "chunk_size": 50, "warnings": []},
            )
        return httpx.Response(404, json={"error": "nope"})

    return httpx.MockTransport(handle)


async def test_smolvla_backend_schema():
    seen: list[dict[str, Any]] = []
    backend = pb.SmolVLABackend("ws://127.0.0.1:8200", transport=smolvla_transport(seen))
    assert backend.url == "http://127.0.0.1:8200"
    await backend.reset("Grab orange and place into plate")
    a = await backend.predict(pb.REST_POSE_USD_DEG, jpeg(), jpeg(), frames_blurred=True)
    b = await backend.predict(a.actions_deg[20], jpeg(), jpeg(), frames_blurred=True, executed=20, delay=3)
    assert set(seen[0]["images"]) == {"front", "wrist"} and seen[0]["state_deg"] == list(pb.REST_POSE_USD_DEG)
    assert seen[0]["reset"] is True and seen[1]["reset"] is False
    assert len(a.actions_deg) == 50 and a.model.startswith("edge-inference") and not b.rtc_used
    assert len(a.rows_to_execute(backend.info.default_execute)) == 20
    await backend.close()


async def test_smolvla_backend_errors():
    backend = pb.SmolVLABackend(transport=smolvla_transport([], status="loading"))
    with pytest.raises(pb.PolicyBackendError) as ei:
        await backend.reset("x")
    assert ei.value.code == "policy_unavailable"
    backend = pb.SmolVLABackend(transport=smolvla_transport([], infer_status=503))
    await backend.reset("x")
    with pytest.raises(pb.PolicyBackendError) as ei:
        await backend.predict(pb.REST_POSE_USD_DEG, jpeg(), jpeg(), frames_blurred=True)
    assert ei.value.code == "policy_unavailable"


async def test_scripted_backend_is_a_policy_backend():
    backend = pb.create_policy_backend("scripted", chunk_size=10)
    assert isinstance(backend, pb.PolicyBackend)
    await backend.reset("pick")
    chunk = await backend.predict([0, -100, 90, 50, 0, -10], b"x", b"y", frames_blurred=True)
    assert len(chunk.actions_deg) == 10 and chunk.actions_deg[0][1] == -97.0 and chunk.backend == "scripted"


def test_sync_wrapper_runs_backend_on_its_own_loop():
    sync = pb.SyncPolicyBackend(lambda: pb.ScriptedBackend(chunk_size=5))
    try:
        sync.reset("pick")
        chunk = sync.predict(list(pb.REST_POSE_USD_DEG), b"x", b"y", frames_blurred=True)
        assert len(chunk.actions_deg) == 5 and sync.info.name == "scripted"
    finally:
        sync.close()


def test_prepare_jpeg_resizes_and_keeps_good_frames():
    from PIL import Image

    ok = jpeg()
    assert pb.prepare_jpeg(ok) is ok
    small = pb.prepare_jpeg(jpeg((320, 240)))
    assert Image.open(io.BytesIO(small)).size == (640, 480)
    with pytest.raises(pb.PolicyBackendError):
        pb.prepare_jpeg(b"not an image")


def test_policy_capability_rows(monkeypatch):
    monkeypatch.setattr(pb, "resolve_api_key", lambda: ("", ""))
    cap = pb.policy_capability("rover-ave-1", "perceptron", "isaac")
    assert not cap.available and pv.KEY_NAME in cap.reason and cap.input_frames_blurred
    monkeypatch.setattr(pb, "resolve_api_key", lambda: (KEY, "env"))
    monkeypatch.setattr(pb, "default_deployment_id", lambda: "dep_1")
    cap = pb.policy_capability("rover-ave-1", "perceptron", "isaac")
    assert cap.available and cap.model_id == "perceptron/dep_1" and cap.tier == "t1_pretrained"
    assert not pb.policy_capability("rover-ave-1", "perceptron", "fake").available
    assert not pb.policy_capability("rover-ave-1", "smolvla", "isaac", reachable=False).available
    assert pb.policy_capability("rover-ave-1", "scripted", "fake").available


def test_create_policy_backend_names(monkeypatch):
    monkeypatch.setenv("PICKY_VLA_POLICY_URL", "")
    assert isinstance(pb.create_policy_backend("smolvla"), pb.SmolVLABackend)
    assert isinstance(pb.create_policy_backend("perceptron_vla", deployment_id="dep_1"), pb.PerceptronBackend)
    with pytest.raises(ValueError):
        pb.create_policy_backend("gr00t")


def test_resolve_key_prefers_env(monkeypatch):
    monkeypatch.setenv(pv.KEY_NAME, "from-env-0123456789")
    monkeypatch.setattr(pv, "_settings_value", lambda name: "")
    assert pv.resolve_api_key() == ("from-env-0123456789", "env")
    assert "from-env" not in pv.mask("from-env-0123456789")


def test_synthetic_frames_are_jpegs():
    side, wrist = pv.synthetic_frames()
    assert side[:2] == b"\xff\xd8" and wrist[:2] == b"\xff\xd8"
