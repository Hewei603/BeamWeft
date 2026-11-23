"""Photo intake produces a reviewable draft, never an inventory mutation."""
import io
import json
from types import SimpleNamespace

from PIL import Image

from app.api import inventory_label
from app.models import InventoryItem

REVIEWER = {"x-user-role": "reviewer"}


def image_file():
    stream = io.BytesIO()
    Image.new("RGB", (16, 16), "white").save(stream, format="PNG")
    return {"file": ("crystal-label.png", stream.getvalue(), "image/png")}


def draft():
    return {"draft": {"name": "标签复合晶体", "category": "gain_crystal", "raw_spec": "YAG+Nd:YAG+YAG+Cr:YAG; T=90%±2%", "crystal_spec": {
        "width_mm": 3, "height_mm": 3, "segments": [
            {"material": "YAG", "role": "undoped", "length_mm": 4},
            {"material": "Nd:YAG", "role": "gain", "length_mm": 10, "doping_pct": None, "uncertain": True, "note": "浓度被遮挡"},
            {"material": "YAG", "role": "undoped", "length_mm": 4},
            {"material": "Cr:YAG", "role": "saturable_absorber", "length_mm": 2},
        ]}}, "warnings": ["端面镀膜看不清"]}


def fake_provider(monkeypatch, payload):
    captured = {}

    async def agent_turn(instructions, messages, tools):
        captured.update(instructions=instructions, messages=messages, tools=tools)
        return {"calls": [{"name": "draft_component", "arguments": json.dumps(payload)}], "_usage": {"provider": "test"}}

    monkeypatch.setattr(inventory_label, "get_ai_provider", lambda: SimpleNamespace(agent_turn=agent_turn))
    return captured


def test_photo_draft_keeps_unknowns_and_does_not_create_inventory(client, db, monkeypatch):
    capture = fake_provider(monkeypatch, draft())
    count = db.query(InventoryItem).count()
    response = client.post("/api/inventory/recognize-label", files=image_file(), headers=REVIEWER)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["saved"] is False
    assert db.query(InventoryItem).count() == count
    assert data["draft"]["crystal_spec"]["segments"][1]["doping_pct"] is None
    assert data["draft"]["coatings"] == []
    assert data["source_filename"] == "crystal-label.png"
    assert capture["messages"][0]["images"][0]["data_url"].startswith("data:image/png;base64,")
    assert capture["tools"][0]["name"] == "draft_component"


def test_wavelength_free_absorber_transmission_is_not_accepted_as_known(client, monkeypatch):
    payload = draft()
    payload["draft"]["crystal_spec"]["segments"][-1]["initial_transmission_pct"] = 90
    fake_provider(monkeypatch, payload)
    response = client.post("/api/inventory/recognize-label", files=image_file(), headers=REVIEWER)
    assert response.status_code == 200, response.text
    segment = response.json()["draft"]["crystal_spec"]["segments"][-1]
    assert segment["initial_transmission_pct"] is None
    assert "90" in segment["note"]
    assert segment["uncertain"]


def test_invalid_photo_never_invokes_model(client, monkeypatch):
    def forbidden():
        raise AssertionError("invalid upload must not invoke the model")
    monkeypatch.setattr(inventory_label, "get_ai_provider", forbidden)
    response = client.post("/api/inventory/recognize-label", files={"file": ("label.jpg", b"not an image", "image/jpeg")}, headers=REVIEWER)
    assert response.status_code == 415


def test_demo_mode_does_not_fabricate_label_reading(client):
    response = client.post("/api/inventory/recognize-label", files=image_file(), headers=REVIEWER)
    assert response.status_code == 503
    assert "演示模式" in response.json()["detail"]


def test_connection_failure_remains_actionable(client, monkeypatch):
    def broken():
        raise RuntimeError("未找到 Codex CLI")
    monkeypatch.setattr(inventory_label, "get_ai_provider", broken)
    response = client.post("/api/inventory/recognize-label", files=image_file(), headers=REVIEWER)
    assert response.status_code == 502
    assert "未找到 Codex" in response.json()["detail"]


def test_absorber_cannot_become_a_clear_endcap_in_photo_draft(client, monkeypatch):
    payload = draft()
    payload["draft"]["crystal_spec"]["segments"][-1]["role"] = "undoped"
    fake_provider(monkeypatch, payload)
    response = client.post("/api/inventory/recognize-label", files=image_file(), headers=REVIEWER)
    assert response.status_code == 502
    assert "没有入库" in response.json()["detail"]
