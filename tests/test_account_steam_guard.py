import base64
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app
import app.accounts as accounts_mod

def test_account_level_steam_guard(tmp_path, monkeypatch):
    test_acc_file = tmp_path / "accounts.json"
    monkeypatch.setattr(accounts_mod, "_ACCOUNTS_FILE", Path(test_acc_file))
    
    client = TestClient(app)
    
    dummy_secret_bytes = b"12345678901234567890"
    dummy_secret_b64 = base64.b64encode(dummy_secret_bytes).decode("utf-8")
    
    # 1. Add account with steam guard info
    resp = client.post("/api/accounts", json={
        "username": "user1",
        "password": "pass1",
        "steam_id": "76561198000000001",
        "display_name": "User One",
        "shared_secret": dummy_secret_b64,
        "identity_secret": "identity_1",
        "device_id": "android:device_1"
    })
    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["ok"] is True
    acc1 = res_data["account"]
    acc1_id = acc1["id"]
    assert acc1["shared_secret"] == dummy_secret_b64
    assert acc1["identity_secret"] == "identity_1"
    assert acc1["device_id"] == "android:device_1"
    
    # Add a second account with different secret
    dummy_secret_bytes_2 = b"abcdefghij0123456789"
    dummy_secret_b64_2 = base64.b64encode(dummy_secret_bytes_2).decode("utf-8")
    resp2 = client.post("/api/accounts", json={
        "username": "user2",
        "password": "pass2",
        "steam_id": "76561198000000002",
        "display_name": "User Two",
        "shared_secret": dummy_secret_b64_2,
        "identity_secret": "identity_2",
        "device_id": "android:device_2"
    })
    assert resp2.status_code == 200
    acc2 = resp2.json()["account"]
    acc2_id = acc2["id"]
    
    # 2. Test api_steam_guard with specific account_id
    resp_sg1 = client.get(f"/api/steam_guard?account_id={acc1_id}")
    assert resp_sg1.status_code == 200
    data_sg1 = resp_sg1.json()
    assert data_sg1["ok"] is True
    assert len(data_sg1["code"]) == 5
    assert data_sg1["account"]["id"] == acc1_id
    
    resp_sg2 = client.get(f"/api/steam_guard?account_id={acc2_id}")
    assert resp_sg2.status_code == 200
    data_sg2 = resp_sg2.json()
    assert data_sg2["ok"] is True
    assert len(data_sg2["code"]) == 5
    assert data_sg2["account"]["id"] == acc2_id
    assert data_sg1["code"] != data_sg2["code"]
    
    # 3. Test updating account credentials
    resp_up = client.put(f"/api/accounts/{acc1_id}", json={
        "shared_secret": dummy_secret_b64_2,
        "device_id": "android:device_1_updated"
    })
    assert resp_up.status_code == 200
    assert resp_up.json()["account"]["shared_secret"] == dummy_secret_b64_2
    assert resp_up.json()["account"]["device_id"] == "android:device_1_updated"
    assert resp_up.json()["account"]["identity_secret"] == "identity_1"

