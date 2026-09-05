from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from faceproof.config import Settings
from faceproof.web import create_app


def test_unified_workspace_and_endpoints(tmp_path: Path) -> None:
    settings = Settings(
        serpapi_api_key="test-key",
        model_dir=tmp_path / "models",
        output_dir=tmp_path / "evidence",
        rpc_url="http://127.0.0.1:8545",
        chain_id=84532,
        contract_address=None,
        private_key=None,
        confirmations=1,
        http_timeout_seconds=2,
    )

    with TestClient(create_app(settings)) as client:
        # 1. Main index
        home = client.get("/")
        assert home.status_code == 200
        assert 'id="workspace"' in home.text
        assert 'id="preflight-status-card"' in home.text
        assert 'id="evidence-history"' in home.text
        assert 'id="photo-copy-form"' in home.text

        # CSRF token
        match = re.search(r'name="faceproof-csrf" content="([^"]+)"', home.text)
        assert match is not None
        csrf = match.group(1)

        # 2. Preflight endpoint responds with CSRF
        preflight_resp = client.post(
            "/api/preflight",
            data={"consent_adult": "true", "consent_authorized": "true"},
            files={"image": ("test.jpg", b"invalid-bytes", "image/jpeg")},
            headers={"X-FaceProof-CSRF": csrf},
        )
        assert preflight_resp.status_code in {400, 503}

        # 3. Assets
        js_resp = client.get("/assets/app.js")
        assert js_resp.status_code == 200
        assert "runPreflightCheck" in js_resp.text
        assert "renderPhotoCopyResult" in js_resp.text

        css_resp = client.get("/assets/app.css")
        assert css_resp.status_code == 200
        assert "preflight-status-card" in css_resp.text

        # 3. API Status endpoints
        readiness = client.get("/api/readiness")
        assert readiness.status_code == 200

        photo_status = client.get("/api/photos/status")
        assert photo_status.status_code == 200
        assert photo_status.json()["search_configured"] is True

        photo_history = client.get("/api/photos/history")
        assert photo_history.status_code == 200
        assert "runs" in photo_history.json()
