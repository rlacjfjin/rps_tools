"""端到端：起一个真实的 FastAPI 应用（含后台工作进程），走 HTTP 提交 / 轮询 / 查询。"""
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import find_experiment, find_export_pairs
from rps_tools.config import ROOT, Settings
from rps_tools.core.resultdb import MISSING, DiffSink, DiffStore, export_item, raw_json
from rps_tools.web.app import create_app

# 依赖本地真实数据的用例：数据目录不进仓库，干净 clone 上会自动跳过
EXPERIMENT = find_experiment()
EXPORT_PAIRS = find_export_pairs()


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    ws = tmp_path_factory.mktemp("ws")
    app = create_app(Settings(workspace=ws, workers=2))
    with TestClient(app) as c:
        yield c


def wait(client, job_id, timeout=180):
    t = time.time()
    while time.time() - t < timeout:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed", "cancelled", "interrupted"):
            return job
        time.sleep(0.3)
    raise AssertionError(f"job {job_id} timeout")


def test_meta_lists_three_groups(client):
    meta = client.get("/api/meta").json()
    assert [g["id"] for g in meta["groups"]] == ["diff", "exp", "diagnose"]
    ids = {t["id"] for t in meta["tools"]}
    assert {"diff.compare", "exp.analyze", "diagnose.material_trace", "diagnose.compare_scenarios"} <= ids


def test_diff_upload_and_query(client):
    a = json.dumps({"materialList": [{"code": "A", "v": 1}, {"code": "B", "v": 2}]})
    b = json.dumps({"materialList": [{"code": "B", "v": 3}, {"code": "A", "v": 1}]})
    r = client.post("/api/tools/diff.compare/jobs",
                    files={"file_a": ("a.json", a), "file_b": ("b.json", b)},
                    data={"format": "auto"})
    assert r.status_code == 200, r.text
    job = wait(client, r.json()["id"])
    assert job["status"] == "succeeded", job
    assert job["result"]["format"] == "json" and job["result"]["total"] == 1
    items = client.get(f"/api/jobs/{job['id']}/diff/items").json()
    assert items["items"][0]["path"] == "materialList[code=B]/v"
    csv = client.get(f"/api/jobs/{job['id']}/diff/export.csv").text
    assert "materialList[code=B]/v" in csv and csv.startswith("﻿")
    log = client.get(f"/api/jobs/{job['id']}/log").json()
    assert "对比方式" in log["text"]


def test_diff_export_json_keeps_full_values(client):
    a = {"rows": [{"code": "A", "text": "x" * 500, "v": 1, "flag": None}], "gone": {"k": 1}}
    b = {"rows": [{"code": "A", "text": "y" * 500, "v": 2.5, "flag": True}], "fresh": [1, 2]}
    r = client.post("/api/tools/diff.compare/jobs",
                    files={"file_a": ("a.json", json.dumps(a)), "file_b": ("b.json", json.dumps(b))},
                    data={"format": "json"})
    job = wait(client, r.json()["id"])
    assert job["status"] == "succeeded", job

    resp = client.get(f"/api/jobs/{job['id']}/diff/export.json")
    assert resp.headers["content-type"].startswith("application/json")
    assert f'filename="diff_{job["id"]}.json"' in resp.headers["content-disposition"]
    data = resp.json()
    assert (data["file_a"], data["file_b"], data["format"]) == ("a.json", "b.json", "json")
    assert (data["total"], data["exported"], data["truncated"], data["complete_values"]) == (5, 5, False, True)
    by_path = {d["path"]: d for d in data["diffs"]}
    assert by_path["rows[code=A]/text"]["new"] == "y" * 500
    assert by_path["rows[code=A]/v"]["old"] == 1 and by_path["rows[code=A]/v"]["new"] == 2.5
    assert by_path["rows[code=A]/v"]["delta"] == 1.5
    assert by_path["rows[code=A]/flag"]["old"] is None and by_path["rows[code=A]/flag"]["new"] is True
    assert by_path["gone"]["old"] == {"k": 1} and "new" not in by_path["gone"]
    assert by_path["fresh"]["new"] == [1, 2] and "old" not in by_path["fresh"]

    filtered = client.get(f"/api/jobs/{job['id']}/diff/export.json", params={"field": "v"}).json()
    assert filtered["filters"] == {"field": "v"} and filtered["exported"] == 1
    assert [d["path"] for d in filtered["diffs"]] == ["rows[code=A]/v"]

    page = client.get(f"/api/jobs/{job['id']}/diff/items").json()["items"]
    assert all("old_json" not in it and "new_json" not in it for it in page)   # 分页接口不带完整大对象
    assert max(len(it["new"] or "") for it in page) < 500


def test_export_item_falls_back_on_jobs_without_full_values(tmp_path):
    db = tmp_path / "old.sqlite"
    sink = DiffSink(db)
    sink.add("changed", "a/v", 1, 2, section="a", field="v")
    sink.add("added", "a/w", MISSING, "x", section="a", field="w")
    sink.close()
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE items DROP COLUMN old_json")
    conn.execute("ALTER TABLE items DROP COLUMN new_json")
    conn.commit()
    conn.close()
    store = DiffStore(db)
    assert store.has_full_values() is False
    items = [json.loads(export_item(r)) for r in store.iter_rows(full=True)]
    assert (items[0]["old"], items[0]["new"]) == ("1", "2")   # 旧库只有展示文本
    assert items[1]["new"] == "x" and "old" not in items[1]


def test_raw_json_nan_is_valid_json():
    assert json.loads(raw_json(float("nan"))) == "nan"
    assert raw_json(MISSING) is None


def test_missing_required_param_is_400(client):
    r = client.post("/api/tools/diff.compare/jobs", data={"format": "auto"})
    assert r.status_code == 400 and "旧文件" in r.json()["detail"]


def test_worker_crash_is_reported(client):
    r = client.post("/api/tools/diff.compare/jobs",
                    files={"file_a": ("a.json", "{bad json"), "file_b": ("b.json", "{}")},
                    data={"format": "json"})
    job = wait(client, r.json()["id"])
    assert job["status"] == "failed" and "JSONDecodeError" in job["error"]


@pytest.mark.skipif(EXPERIMENT is None, reason="experiments/ 下没有本地实验数据（数据不进仓库）")
def test_exp_analyzer_on_server_path(client):
    r = client.post("/api/tools/exp.analyze/jobs", data={"exp_dir": str(EXPERIMENT)})
    assert r.status_code == 200, r.text
    job = wait(client, r.json()["id"], timeout=300)
    assert job["status"] == "succeeded", job["error"]
    assert "OBJ" in job["result"]["kpis"]
    files = {f["path"] for f in client.get(f"/api/jobs/{job['id']}/files").json()}
    assert {"report.md", "delta_chain_minimal.html", "obj_contributions.csv"} <= files
    html = client.get(f"/api/jobs/{job['id']}/markdown/report.md").text
    assert "<table>" in html


@pytest.mark.skipif(len(EXPORT_PAIRS) < 2, reason="export/ 下没有两组本地样例数据（数据不进仓库）")
def test_compare_scenarios_text_output(client):
    (in_a, out_a), (in_b, out_b) = EXPORT_PAIRS[:2]
    r = client.post("/api/tools/diagnose.compare_scenarios/jobs", data={
        "before__path": str(out_a), "after__path": str(out_b),
        "before_in__path": str(in_a), "after_in__path": str(in_b),
    })
    assert r.status_code == 200, r.text
    job = wait(client, r.json()["id"], timeout=300)
    assert job["status"] == "succeeded", job["error"]
    out = client.get(f"/api/jobs/{job['id']}/files/output.txt").text
    assert "OBJ" in out and job["result"]["lines"] > 10


def test_diagnose_missing_files_fails_cleanly(client):
    r = client.post("/api/tools/diagnose.parse_expressions/jobs", data={})
    job = wait(client, r.json()["id"])
    assert job["status"] == "failed" and "proto_expression" in job["error"]


def test_restricted_paths(tmp_path):
    data = tmp_path / "data"
    (data / "case1").mkdir(parents=True)
    s = Settings(host="0.0.0.0", workspace=tmp_path / "ws", data_roots=[data])
    assert s.is_restricted
    s.resolve_path(str(data / "case1"), "dir")
    with pytest.raises(ValueError):
        s.resolve_path(str(Path(ROOT / "rps_tools").resolve()), "dir")
    assert not Settings(host="127.0.0.1", workspace=tmp_path / "ws").is_restricted
