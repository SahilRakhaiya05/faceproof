from __future__ import annotations

import json
from pathlib import Path

from faceproof.reporting import list_run_summaries, summarize_run


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_summary_is_presentation_safe_and_separates_profile_leads(tmp_path: Path) -> None:
    run = tmp_path / "evidence" / "run-1"
    _write(
        run / "input" / "face-encoding.json",
        {
            "detection": {"confidence": "0.98", "box": {"width": "120"}},
            "quality": {"sharpness": "140", "brightness": "110"},
        },
    )
    (run / "input" / "query.jpg").write_bytes(b"query")
    post = {
        "rank": 1,
        "normalized_url": "https://x.com/person/status/123",
        "title": "Consented post",
        "source": "X",
        "post_id": "123",
    }
    profile = {
        "rank": 2,
        "normalized_url": "https://linkedin.com/in/consented-person",
        "title": "Profile lead",
        "source": "LinkedIn",
    }
    _write(
        run / "search" / "provider-response.json",
        {
            "provider": "serpapi",
            "search_id": "lens-1",
            "search_ids": ["lens-1"],
            "search_types": ["all"],
            "live": True,
            "candidates": [post, profile],
            "web_labels": ["Unverified label"],
            "raw_response": {"api_key": "must-not-leak"},
        },
    )
    _write(
        run / "candidates" / "01" / "assessment.json",
        {
            "candidate": post,
            "status": "matched",
            "local_similarity_micros": 900_000,
            "threshold_micros": 363_000,
        },
    )
    _write(
        run / "profile-leads" / "01" / "assessment.json",
        {
            "candidate": profile,
            "status": "face-match-lead",
            "claim": "unverified-public-profile-lead",
            "eligible_for_anchor": False,
            "local_similarity_micros": 700_000,
            "threshold_micros": 363_000,
        },
    )
    _write(
        run / "selection.json",
        {
            "candidate": post,
            "local_similarity_micros": 900_000,
            "threshold_micros": 363_000,
            "linkage_level": "captured-post-media-rematched",
        },
    )
    _write(run / "manifest.json", {"artifacts": [], "canonicalization": {}})
    _write(
        run / "commitment.json",
        {"manifest_sha256": "0x" + "11" * 32, "commitment": "0x" + "22" * 32},
    )

    summary = summarize_run(run)

    assert summary["status"] == "discovered"
    assert summary["selected"]["similarity"] == 0.9
    assert summary["profile_leads"][0]["eligible_for_anchor"] is False
    assert summary["profile_leads"][0]["claim"] == "unverified-public-profile-lead"
    assert "must-not-leak" not in json.dumps(summary)
    assert len(list_run_summaries(tmp_path / "evidence")) == 1


def test_summary_uses_anchor_receipt_field_names(tmp_path: Path) -> None:
    run = tmp_path / "run-2"
    _write(run / "manifest.json", {"artifacts": []})
    _write(run / "commitment.json", {"commitment": "0x" + "22" * 32})
    _write(
        run / "chain-receipt.json",
        {
            "chain_id": 84532,
            "rpc_network": "base-sepolia",
            "contract_address": "0x" + "33" * 20,
            "transaction_hash": "0x" + "44" * 32,
            "block_number": 123,
            "block_hash": "0x" + "55" * 32,
            "confirmations_observed": 2,
            "submitter": "0x" + "66" * 20,
            "chain_timestamp": 1_700_000_000,
        },
    )

    summary = summarize_run(run)

    assert summary["status"] == "anchor-recorded"
    assert summary["chain"]["network"] == "base-sepolia"
    assert summary["chain"]["confirmations"] == 2
    assert "sepolia.basescan.org/tx/" in summary["chain"]["explorer_url"]


def test_summary_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    run = tmp_path / "duplicate-run"
    run.mkdir()
    (run / "manifest.json").write_text(
        '{"artifacts": [], "artifacts": [{"path": "hidden"}]}',
        encoding="utf-8",
    )
    _write(run / "commitment.json", {"commitment": "0x" + "22" * 32})

    summary = summarize_run(run)

    assert summary["status"] == "interrupted"
    assert summary["integrity"]["artifact_count"] == 0


def test_summary_surfaces_out_of_bundle_pending_anchor_journal(tmp_path: Path) -> None:
    run = tmp_path / "pending-run"
    _write(run / "manifest.json", {"artifacts": []})
    _write(run / "commitment.json", {"commitment": "0x" + "22" * 32})
    _write(
        tmp_path / ".pending-run.anchor-submission.json",
        {
            "transaction_hash": "0x" + "33" * 32,
            "chain_id": 84532,
            "contract_address": "0x" + "44" * 20,
            "submitter": "0x" + "55" * 20,
            "nonce": 8,
        },
    )

    summary = summarize_run(run)

    assert summary["status"] == "anchor-pending"
    assert summary["anchor_pending"]["nonce"] == 8
    assert summary["anchor_pending"]["transaction_hash"] == "0x" + "33" * 32
