from __future__ import annotations


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_models_are_european(client):
    r = client.get("/models")
    assert r.status_code == 200
    models = r.json()
    ids = {m["id"] for m in models}
    # The five/six European presets from the plan.
    assert {"ecmwf", "icon", "icon_eu", "arpege", "arome", "ukmo"} <= ids
    # Region lock: no US models anywhere.
    for m in models:
        assert m["id"] not in {"gfs", "hrrr", "nam", "nbm"}
        assert m["color"].startswith("#")
