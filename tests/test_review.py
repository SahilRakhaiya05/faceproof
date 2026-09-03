from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import faceproof.review as review_module
from faceproof.config import Settings
from faceproof.review import ReviewError, load_reviewed_discovery


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        serpapi_api_key=None,
        model_dir=tmp_path / "models",
        output_dir=tmp_path / "evidence",
        rpc_url="http://127.0.0.1:8545",
        chain_id=84532,
        contract_address=None,
        private_key=None,
        confirmations=1,
        http_timeout_seconds=1,
        contract_code_hash=None,
        source_revision=None,
    )


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _review_bundle(settings: Settings) -> Path:
    run = settings.output_dir / "reviewed-run"
    (run / "input").mkdir(parents=True)
    (run / "input" / "query.jpg").write_bytes(b"sealed-image")
    _write(
        run / "selection.json",
        {"candidate": {"normalized_url": "https://x.com/person/status/123"}},
    )
    _write(
        run / "manifest.json",
        {
            "metadata": {
                "search": {
                    "platform_filter": ["x", "reddit"],
                    "platform_filter_mode": "explicit",
                    "search_mode": "deep",
                    "post_candidate_limit": 9,
                    "profile_candidate_limit": 6,
                },
                "consent": {
                    "profile_discovery_authorized": True,
                    "profile_platforms": ["linkedin"],
                },
            }
        },
    )
    _write(
        run / "commitment.json",
        {
            "manifest_sha256": "0x" + "11" * 32,
            "commitment": "0x" + "22" * 32,
        },
    )
    return run


def test_load_reviewed_discovery_replays_sealed_input_and_policy(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    run = _review_bundle(settings)
    monkeypatch.setattr(
        review_module,
        "verify_run",
        lambda *_args, **_kwargs: SimpleNamespace(passed=True),
    )

    reviewed = load_reviewed_discovery(
        output_dir=settings.output_dir,
        run_id=run.name,
        approved_post_url="https://x.com/person/status/123?utm_source=test",
        settings=settings,
    )

    assert reviewed.image_path.read_bytes() == b"sealed-image"
    assert reviewed.search_mode == "deep"
    assert reviewed.platforms == frozenset({"x", "reddit"})
    assert reviewed.max_candidates == 9
    assert reviewed.max_profile_candidates == 6
    assert reviewed.profile_discovery_authorized is True
    assert reviewed.profile_platforms == frozenset({"linkedin"})


def test_load_reviewed_discovery_rejects_different_permalink(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    _review_bundle(settings)
    monkeypatch.setattr(
        review_module,
        "verify_run",
        lambda *_args, **_kwargs: SimpleNamespace(passed=True),
    )

    with pytest.raises(ReviewError, match="does not match"):
        load_reviewed_discovery(
            output_dir=settings.output_dir,
            run_id="reviewed-run",
            approved_post_url="https://x.com/person/status/999",
            settings=settings,
        )


def test_load_reviewed_discovery_rejects_inconsistent_profile_consent(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    run = _review_bundle(settings)
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    manifest["metadata"]["consent"]["profile_discovery_authorized"] = False
    _write(run / "manifest.json", manifest)
    monkeypatch.setattr(
        review_module,
        "verify_run",
        lambda *_args, **_kwargs: SimpleNamespace(passed=True),
    )

    with pytest.raises(ReviewError, match="inconsistent"):
        load_reviewed_discovery(
            output_dir=settings.output_dir,
            run_id="reviewed-run",
            approved_post_url="https://x.com/person/status/123",
            settings=settings,
        )


def test_load_reviewed_discovery_rejects_duplicate_json_keys(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    run = _review_bundle(settings)
    (run / "selection.json").write_text(
        '{"candidate":{"normalized_url":"https://x.com/person/status/123"},'
        '"candidate":{"normalized_url":"https://x.com/person/status/999"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        review_module,
        "verify_run",
        lambda *_args, **_kwargs: SimpleNamespace(passed=True),
    )

    with pytest.raises(ReviewError, match="Duplicate JSON key"):
        load_reviewed_discovery(
            output_dir=settings.output_dir,
            run_id="reviewed-run",
            approved_post_url="https://x.com/person/status/123",
            settings=settings,
        )
