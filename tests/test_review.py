from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import faceproof.review as review_module
from faceproof.config import Settings
from faceproof.pipeline import ReviewedContentIdentity, review_anchor_claim_path
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


def _lens_content_identity(
    *,
    url: str = "https://x.com/person/status/123",
    candidate_media_sha256: str,
) -> ReviewedContentIdentity:
    return ReviewedContentIdentity(
        search_provider="lens",
        normalized_url=url,
        post_id="123",
        candidate_media_sha256=candidate_media_sha256,
        capture_method="x-oembed",
        capture_content_sha256="44" * 32,
        capture_media_sha256=(),
    )


def _review_bundle(settings: Settings) -> Path:
    run = settings.output_dir / "reviewed-run"
    (run / "input").mkdir(parents=True)
    (run / "input" / "query.jpg").write_bytes(b"sealed-image")
    candidate_path = run / "candidates" / "01" / "candidate.jpg"
    candidate_path.parent.mkdir(parents=True)
    candidate_path.write_bytes(b"reviewed-candidate-media")
    candidate_hash = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    identity = _lens_content_identity(candidate_media_sha256=candidate_hash)
    candidate = {"normalized_url": "https://x.com/person/status/123"}
    _write(
        run / "selection.json",
        {
            "candidate": candidate,
            "candidate_media": {"sha256": candidate_hash},
            "threshold_micros": 417_000,
            "reviewed_content_identity": identity.to_dict(),
            "reviewed_content_identity_sha256": identity.sha256,
        },
    )
    _write(
        run / "manifest.json",
        {
            "artifacts": [
                {
                    "path": "input/query.jpg",
                    "size": len(b"sealed-image"),
                    "sha256": hashlib.sha256(b"sealed-image").hexdigest(),
                },
                {
                    "path": "candidates/01/candidate.jpg",
                    "size": candidate_path.stat().st_size,
                    "sha256": candidate_hash,
                },
            ],
            "metadata": {
                "search": {
                    "provider": "serpapi",
                    "provider_strategy": "lens",
                    "search_id": "lens-exact",
                    "search_ids": ["lens-exact", "lens-visual"],
                    "search_types": ["exact_matches", "visual_matches"],
                    "retrieved_at": "2026-09-03T10:00:00Z",
                    "live": True,
                    "provider_mode": "no-cache",
                    "platform_filter": ["x", "reddit"],
                    "platform_filter_mode": "explicit",
                    "search_mode": "deep",
                    "post_candidate_limit": 9,
                    "profile_candidate_limit": 6,
                },
                "selection": {
                    "threshold_micros": 417_000,
                    "reviewed_content_identity": identity.to_dict(),
                    "reviewed_content_identity_sha256": identity.sha256,
                },
                "consent": {
                    "profile_discovery_authorized": True,
                    "profile_platforms": ["linkedin"],
                },
            },
        },
    )
    _write(
        run / "search" / "provider-response.json",
        {
            "provider": "serpapi",
            "search_id": "lens-exact",
            "search_ids": ["lens-exact", "lens-visual"],
            "search_types": ["exact_matches", "visual_matches"],
            "retrieved_at": "2026-09-03T10:00:00Z",
            "live": True,
            "provider_mode": "no-cache",
            "candidates": [candidate],
            "raw_response": {
                "search_mode": "deep",
                "requests": [
                    {
                        "engine": "google_lens",
                        "type": "exact_matches",
                        "no_cache": "true",
                    },
                    {
                        "engine": "google_lens",
                        "type": "visual_matches",
                        "no_cache": "true",
                    },
                ],
            },
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


def _set_bluesky_selection(run: Path, permalink: str) -> dict:
    selection = json.loads((run / "selection.json").read_text(encoding="utf-8"))
    candidate_hash = selection["candidate_media"]["sha256"]
    post_cid = "b" + "a" * 23
    image_cid = "b" + "c" * 23
    at_uri = "at://did:plc:consented/app.bsky.feed.post/3kexample"
    candidate = {
        "normalized_url": permalink,
        "provider_item_id": f"{at_uri}|{post_cid}|{image_cid}",
    }
    identity = ReviewedContentIdentity(
        search_provider="bluesky",
        normalized_url=permalink,
        post_id="did:plc:consented/3kexample",
        candidate_media_sha256=candidate_hash,
        capture_method="bluesky-public-api",
        capture_content_sha256="55" * 32,
        capture_media_sha256=(candidate_hash,),
        bluesky_at_uri=at_uri,
        bluesky_post_cid=post_cid,
        bluesky_image_cid=image_cid,
    )
    selection.update(
        {
            "candidate": candidate,
            "reviewed_content_identity": identity.to_dict(),
            "reviewed_content_identity_sha256": identity.sha256,
        }
    )
    _write(run / "selection.json", selection)
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    manifest["metadata"]["selection"].update(
        {
            "reviewed_content_identity": identity.to_dict(),
            "reviewed_content_identity_sha256": identity.sha256,
        }
    )
    _write(run / "manifest.json", manifest)
    return candidate


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
    assert reviewed.input_bytes == b"sealed-image"
    assert reviewed.search_mode == "deep"
    assert reviewed.search_provider == "lens"
    assert reviewed.bluesky_actor is None
    assert reviewed.threshold == 0.417
    assert reviewed.platforms == frozenset({"x", "reddit"})
    assert reviewed.max_candidates == 9
    assert reviewed.max_profile_candidates == 6
    assert reviewed.profile_discovery_authorized is True
    assert reviewed.profile_platforms == frozenset({"linkedin"})
    assert reviewed.input_sha256 == hashlib.sha256(b"sealed-image").hexdigest()
    assert reviewed.content_identity.search_provider == "lens"
    assert reviewed.content_identity.normalized_url == "https://x.com/person/status/123"


def test_review_authorization_uses_one_verified_snapshot_when_source_is_replaced(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    run = _review_bundle(settings)

    def verify_then_replace(snapshot: Path, **_kwargs):
        assert snapshot.resolve() != run.resolve()
        replacement = b"coherent-but-unreviewed-replacement"
        (run / "input" / "query.jpg").write_bytes(replacement)
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        input_record = next(
            item for item in manifest["artifacts"] if item["path"] == "input/query.jpg"
        )
        input_record["size"] = len(replacement)
        input_record["sha256"] = hashlib.sha256(replacement).hexdigest()
        _write(run / "manifest.json", manifest)
        return SimpleNamespace(passed=True)

    monkeypatch.setattr(review_module, "verify_run", verify_then_replace)

    reviewed = load_reviewed_discovery(
        output_dir=settings.output_dir,
        run_id=run.name,
        approved_post_url="https://x.com/person/status/123",
        settings=settings,
    )

    assert reviewed.input_bytes == b"sealed-image"
    assert reviewed.input_sha256 == hashlib.sha256(b"sealed-image").hexdigest()
    assert reviewed.image_path.read_bytes() == b"coherent-but-unreviewed-replacement"


def test_load_reviewed_discovery_replays_bluesky_actor_and_policy(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    run = _review_bundle(settings)
    permalink = "https://bsky.app/profile/did:plc:consented/post/3kexample"
    candidate = _set_bluesky_selection(run, permalink)
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    manifest["metadata"]["search"] = {
        "provider": "bluesky-public-api",
        "provider_strategy": "bluesky",
        "consented_actor": "volunteer.bsky.social",
        "resolved_actor_did": "did:plc:consented",
        "platform_filter": ["bluesky"],
        "platform_filter_mode": "explicit",
        "search_mode": "standard",
        "post_candidate_limit": 12,
        "profile_candidate_limit": 0,
    }
    manifest["metadata"]["consent"] = {
        "profile_discovery_authorized": False,
        "profile_platforms": [],
    }
    _write(run / "manifest.json", manifest)
    _write(
        run / "search" / "provider-response.json",
        {
            "provider": "bluesky-public-api",
            "candidates": [candidate],
            "raw_response": {
                "actor": "volunteer.bsky.social",
                "resolved_actor_did": "did:plc:consented",
            },
        },
    )
    monkeypatch.setattr(
        review_module,
        "verify_run",
        lambda *_args, **_kwargs: SimpleNamespace(passed=True),
    )

    reviewed = load_reviewed_discovery(
        output_dir=settings.output_dir,
        run_id=run.name,
        approved_post_url=permalink,
        settings=settings,
    )

    assert reviewed.search_provider == "bluesky"
    assert reviewed.bluesky_actor == "did:plc:consented"
    assert reviewed.search_mode == "standard"
    assert reviewed.platforms == frozenset({"bluesky"})
    assert reviewed.max_profile_candidates == 0
    assert reviewed.threshold == 0.417
    assert reviewed.content_identity.bluesky_at_uri == (
        "at://did:plc:consented/app.bsky.feed.post/3kexample"
    )


def test_load_reviewed_discovery_rejects_inconsistent_threshold(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    run = _review_bundle(settings)
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    manifest["metadata"]["selection"]["threshold_micros"] = 500_000
    _write(run / "manifest.json", manifest)
    monkeypatch.setattr(
        review_module,
        "verify_run",
        lambda *_args, **_kwargs: SimpleNamespace(passed=True),
    )

    with pytest.raises(ReviewError, match="threshold evidence is inconsistent"):
        load_reviewed_discovery(
            output_dir=settings.output_dir,
            run_id=run.name,
            approved_post_url="https://x.com/person/status/123",
            settings=settings,
        )


def test_load_reviewed_discovery_rejects_cached_lens_provider_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    run = _review_bundle(settings)
    provider_record = json.loads(
        (run / "search" / "provider-response.json").read_text(encoding="utf-8")
    )
    provider_record["live"] = False
    provider_record["provider_mode"] = "cache-allowed"
    _write(run / "search" / "provider-response.json", provider_record)
    monkeypatch.setattr(
        review_module,
        "verify_run",
        lambda *_args, **_kwargs: SimpleNamespace(passed=True),
    )

    with pytest.raises(ReviewError, match="provider evidence is inconsistent"):
        load_reviewed_discovery(
            output_dir=settings.output_dir,
            run_id=run.name,
            approved_post_url="https://x.com/person/status/123",
            settings=settings,
        )


def test_load_reviewed_discovery_rejects_bluesky_handle_reassignment(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    run = _review_bundle(settings)
    permalink = "https://bsky.app/profile/did:plc:consented/post/3kexample"
    candidate = _set_bluesky_selection(run, permalink)
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    manifest["metadata"]["search"].update(
        {
            "provider": "bluesky-public-api",
            "provider_strategy": "bluesky",
            "consented_actor": "volunteer.bsky.social",
            "resolved_actor_did": "did:plc:consented",
            "platform_filter": ["bluesky"],
            "platform_filter_mode": "explicit",
            "search_mode": "standard",
            "profile_candidate_limit": 0,
        }
    )
    manifest["metadata"]["consent"] = {
        "profile_discovery_authorized": False,
        "profile_platforms": [],
    }
    _write(run / "manifest.json", manifest)
    _write(
        run / "search" / "provider-response.json",
        {
            "provider": "bluesky-public-api",
            "candidates": [candidate],
            "raw_response": {
                "actor": "volunteer.bsky.social",
                "resolved_actor_did": "did:plc:reassigned",
            },
        },
    )
    monkeypatch.setattr(
        review_module,
        "verify_run",
        lambda *_args, **_kwargs: SimpleNamespace(passed=True),
    )

    with pytest.raises(ReviewError, match="provider evidence is inconsistent"):
        load_reviewed_discovery(
            output_dir=settings.output_dir,
            run_id=run.name,
            approved_post_url=permalink,
            settings=settings,
        )


def test_load_reviewed_discovery_rejects_bluesky_cid_identity_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    run = _review_bundle(settings)
    permalink = "https://bsky.app/profile/did:plc:consented/post/3kexample"
    candidate = _set_bluesky_selection(run, permalink)
    selection = json.loads((run / "selection.json").read_text(encoding="utf-8"))
    identity_record = selection["reviewed_content_identity"]
    identity_record["bluesky_post_cid"] = "b" + "d" * 23
    changed_identity = ReviewedContentIdentity.from_dict(identity_record)
    selection["reviewed_content_identity"] = changed_identity.to_dict()
    selection["reviewed_content_identity_sha256"] = changed_identity.sha256
    _write(run / "selection.json", selection)

    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    manifest["metadata"]["search"] = {
        "provider": "bluesky-public-api",
        "provider_strategy": "bluesky",
        "consented_actor": "volunteer.bsky.social",
        "resolved_actor_did": "did:plc:consented",
        "platform_filter": ["bluesky"],
        "platform_filter_mode": "explicit",
        "search_mode": "standard",
        "post_candidate_limit": 12,
        "profile_candidate_limit": 0,
    }
    manifest["metadata"]["consent"] = {
        "profile_discovery_authorized": False,
        "profile_platforms": [],
    }
    manifest["metadata"]["selection"].update(
        {
            "reviewed_content_identity": changed_identity.to_dict(),
            "reviewed_content_identity_sha256": changed_identity.sha256,
        }
    )
    _write(run / "manifest.json", manifest)
    _write(
        run / "search" / "provider-response.json",
        {
            "provider": "bluesky-public-api",
            "candidates": [candidate],
            "raw_response": {
                "actor": "volunteer.bsky.social",
                "resolved_actor_did": "did:plc:consented",
            },
        },
    )
    monkeypatch.setattr(
        review_module,
        "verify_run",
        lambda *_args, **_kwargs: SimpleNamespace(passed=True),
    )

    with pytest.raises(ReviewError, match="Bluesky content identity is inconsistent"):
        load_reviewed_discovery(
            output_dir=settings.output_dir,
            run_id=run.name,
            approved_post_url=permalink,
            settings=settings,
        )


def test_load_reviewed_discovery_rejects_a_single_use_anchor_claim(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    run = _review_bundle(settings)
    claim = review_anchor_claim_path(settings.output_dir, run.name)
    claim.write_text('{"single_use":true}', encoding="utf-8")
    monkeypatch.setattr(
        review_module,
        "verify_run",
        lambda *_args, **_kwargs: SimpleNamespace(passed=True),
    )

    with pytest.raises(ReviewError, match="already authorized an anchor attempt"):
        load_reviewed_discovery(
            output_dir=settings.output_dir,
            run_id=run.name,
            approved_post_url="https://x.com/person/status/123",
            settings=settings,
        )


def test_load_reviewed_discovery_rejects_an_unresolved_anchor_run(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    run = _review_bundle(settings)
    (settings.output_dir / ".reviewed-run.anchor-submission.json").write_text(
        '{"schema":"pending"}', encoding="utf-8"
    )
    monkeypatch.setattr(
        review_module,
        "verify_run",
        lambda *_args, **_kwargs: SimpleNamespace(passed=True),
    )

    with pytest.raises(ReviewError, match="unresolved transaction"):
        load_reviewed_discovery(
            output_dir=settings.output_dir,
            run_id=run.name,
            approved_post_url="https://x.com/person/status/123",
            settings=settings,
        )


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
