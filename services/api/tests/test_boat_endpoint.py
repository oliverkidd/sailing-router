from __future__ import annotations

import io
import json

CSV = b"""twa/tws,40,52,90,120,150,180
6,0,4.6,5.8,5.5,4.3,3.2
10,0,6.4,7.5,7.3,6.2,4.9
20,0,7.6,9.6,10.9,10.8,9.1
"""


def test_upload_and_fetch_polar(client):
    files = {"file": ("mypolar.csv", io.BytesIO(CSV), "text/csv")}
    data = {"config": json.dumps({"upwind_twa_deg": 42.0, "polar_efficiency": 0.95})}
    r = client.post("/boat", files=files, data=data)
    assert r.status_code == 200, r.text
    body = r.json()
    pid = body["polar_id"]
    assert pid.startswith("mypolar-")
    assert body["boat"]["upwind_twa_deg"] == 42.0
    assert body["boat"]["polar_efficiency"] == 0.95

    curve = client.get(f"/boat/polar/{pid}").json()
    assert len(curve["tws_axis"]) == 3
    assert len(curve["speed"]) == 3
    assert "vmg_targets" in curve


def test_sample_boat(client):
    r = client.get("/boat/sample")
    assert r.status_code == 200, r.text
    assert r.json()["polar_id"]


def test_invalid_polar_rejected(client):
    files = {"file": ("bad.csv", io.BytesIO(b"not,a,valid\npolar"), "text/csv")}
    r = client.post("/boat", files=files, data={"config": "{}"})
    assert r.status_code == 422
