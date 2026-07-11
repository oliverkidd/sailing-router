"""Tests for model catalog coverage + key-gating."""

from __future__ import annotations

from app.config import MODEL_PRESETS_BY_ID, model_is_configured, models_available_for_bbox
from app.schemas import BBox


def test_global_models_always_available():
    bbox = BBox(lat_min=-10, lon_min=-10, lat_max=-9, lon_max=-9)
    ids = {m.id for m in models_available_for_bbox(bbox)}
    assert "ecmwf" in ids
    assert "gfs" in ids
    assert "aifs" in ids
    assert "gem" in ids
    # Regional should be excluded
    assert "icon_d2" not in ids
    assert "arome" not in ids


def test_icon_d2_only_in_central_europe():
    uk = BBox(lat_min=50.5, lon_min=-2, lat_max=52, lon_max=1)
    ids = {m.id for m in models_available_for_bbox(uk)}
    assert "icon_d2" in ids
    assert "icon_eu" in ids


def test_key_gated_models_hidden_without_env(monkeypatch):
    monkeypatch.delenv("METEOFRANCE_API_KEY", raising=False)
    monkeypatch.delenv("KNMI_API_KEY", raising=False)
    assert model_is_configured("ecmwf")
    assert not model_is_configured("arome")
    assert not model_is_configured("harmonie")
    france = BBox(lat_min=45.0, lon_min=0.0, lat_max=46.0, lon_max=1.0)
    ids = {m.id for m in models_available_for_bbox(france)}
    assert "arome" not in ids
    assert "harmonie" not in ids

    monkeypatch.setenv("METEOFRANCE_API_KEY", "test-key")
    assert model_is_configured("arome")
    # Still may not appear if connector not registered — but config gate opens.
    assert MODEL_PRESETS_BY_ID["arome"].connector == "mf-arome"
