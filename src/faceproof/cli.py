from __future__ import annotations

import platform
import sys
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .chain import ChainError, make_web3, registry_contract
from .config import Settings
from .face import FaceError, OpenCVFaceBackend
from .model_assets import ModelDownloadError, download_default_models, verify_default_models
from .pipeline import (
    InconclusiveError,
    PipelineError,
    run_pipeline,
    run_tamper_demo,
    verify_run,
)
from .provenance import ProvenanceError, verify_git_source_revision
from .search import SearchError, check_serpapi_account

app = typer.Typer(
    name="faceproof",
    help="Consent-first face-to-web discovery with blockchain verification.",
    no_args_is_help=True,
    add_completion=False,
)
models_app = typer.Typer(help="Download and verify pinned OpenCV model assets.")
app.add_typer(models_app, name="models")
console = Console()


def _settings() -> Settings:
    load_dotenv(override=False)
    return Settings.from_env()


def _require_consent(value: bool) -> None:
    if not value:
        console.print(
            "[red]Stopped:[/red] pass --i-have-consent only for an adult who explicitly "
            "authorized this face and public-post search."
        )
        raise typer.Exit(code=2)


@app.command()
def version() -> None:
    """Print the installed FaceProof version."""
    console.print(f"FaceProof {__version__}")


@models_app.command("download")
def download_models(
    force: bool = typer.Option(False, "--force", help="Re-download verified model files."),
) -> None:
    """Download pinned YuNet and SFace models and verify SHA-256 hashes."""
    settings = _settings()
    try:
        paths = download_default_models(
            settings.model_dir,
            force=force,
            on_progress=lambda message: console.print(f"[cyan]>[/cyan] {message}"),
        )
    except ModelDownloadError as exc:
        console.print(f"[red]Model download failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Verified {len(paths)} model files in {settings.model_dir}[/green]")


@models_app.command("verify")
def verify_models() -> None:
    """Verify local model files against pinned official SHA-256 hashes."""
    settings = _settings()
    results = verify_default_models(settings.model_dir)
    table = Table(title="Model integrity")
    table.add_column("Model")
    table.add_column("Status")
    ok = True
    for name, status in results.items():
        passed = status == "ok"
        ok = ok and passed
        table.add_row(name, "[green]PASS[/green]" if passed else f"[red]{status}[/red]")
    console.print(table)
    if not ok:
        raise typer.Exit(code=1)


@app.command()
def doctor(
    check_search: bool = typer.Option(
        True,
        "--check-search/--no-check-search",
        help="Validate the SerpApi key and remaining quota without using a search credit.",
    ),
    check_rpc: bool = typer.Option(
        True, "--check-rpc/--no-check-rpc", help="Check the configured RPC and contract."
    ),
    demo: bool = typer.Option(
        False,
        "--demo",
        help="Require provider, funded wallet, trusted contract, and pinned runtime code.",
    ),
) -> None:
    """Check local models, provider configuration, and blockchain connectivity."""
    settings = _settings()
    table = Table(title="FaceProof readiness")
    table.add_column("Component")
    table.add_column("Status")
    table.add_column("Detail")

    py_ok = sys.version_info >= (3, 11)
    table.add_row(
        "Python",
        _status(py_ok),
        f"{platform.python_version()} ({Path(sys.executable).name})",
    )
    model_results = verify_default_models(settings.model_dir)
    models_ok = all(status == "ok" for status in model_results.values())
    table.add_row(
        "YuNet + SFace",
        _status(models_ok),
        "verified" if models_ok else "run: faceproof models download",
    )
    serpapi_ok = bool(settings.serpapi_api_key)
    serpapi_detail = "configured; account not checked"
    if serpapi_ok and (check_search or demo):
        try:
            account = check_serpapi_account(
                settings.require_serpapi_key(),
                timeout_seconds=min(settings.http_timeout_seconds, 10),
            )
        except (SearchError, ValueError):
            serpapi_ok = False
            serpapi_detail = "account validation failed"
        else:
            serpapi_ok = account.ready
            serpapi_detail = (
                f"{account.plan_name}; {account.searches_left}/"
                f"{account.searches_per_month} searches remaining"
            )
            if not account.ready:
                serpapi_detail += "; account inactive or quota exhausted"
    elif not serpapi_ok:
        serpapi_detail = "SERPAPI_API_KEY not set"
    table.add_row(
        "SerpApi Google Lens",
        _status(serpapi_ok),
        serpapi_detail,
    )

    chain_configured = bool(settings.contract_address and settings.private_key)
    table.add_row(
        "Chain write",
        _status(chain_configured, optional=True),
        (
            f"contract {settings.contract_address}"
            if chain_configured
            else "contract address/private key not fully configured"
        ),
    )
    table.add_row(
        "Registry code pin",
        _status(bool(settings.contract_code_hash), optional=not demo),
        "configured" if settings.contract_code_hash else "FACEPROOF_CONTRACT_CODE_HASH not set",
    )
    revision_ok = bool(settings.source_revision)
    revision_detail = (
        settings.source_revision if revision_ok else "FACEPROOF_SOURCE_REVISION not set"
    )
    if demo and revision_ok:
        try:
            verified_revision = verify_git_source_revision(settings.source_revision)
        except ProvenanceError as exc:
            revision_ok = False
            revision_detail = str(exc)
        else:
            revision_detail = f"{verified_revision.revision}; working tree clean"
    table.add_row(
        "Source revision",
        _status(revision_ok, optional=not demo),
        revision_detail,
    )

    rpc_ok = not check_rpc
    rpc_detail = "not checked"
    wallet_funded = False
    if check_rpc:
        try:
            web3 = make_web3(settings.rpc_url, timeout_seconds=10)
            rpc_ok = web3.is_connected()
            if rpc_ok:
                actual_chain = int(web3.eth.chain_id)
                rpc_ok = actual_chain == settings.chain_id
                rpc_detail = f"chain {actual_chain}; expected {settings.chain_id}"
                if settings.contract_address:
                    registry_contract(
                        web3,
                        settings.contract_address,
                        expected_code_hash=settings.contract_code_hash,
                    )
                    rpc_detail += (
                        "; registry runtime hash verified"
                        if settings.contract_code_hash
                        else "; contract code present, runtime hash unpinned"
                    )
                if settings.private_key:
                    account = web3.eth.account.from_key(settings.private_key)
                    balance = int(web3.eth.get_balance(account.address))
                    wallet_funded = balance > 0
                    rpc_detail += "; wallet funded" if wallet_funded else "; wallet empty"
            else:
                rpc_detail = "connection failed"
        except (ChainError, ValueError):
            rpc_ok = False
            rpc_detail = "configured chain or registry validation failed"
        except Exception as exc:
            # Do not echo RPC URLs or provider error bodies, which may contain keys.
            rpc_ok = False
            rpc_detail = f"connection check failed ({type(exc).__name__})"
    table.add_row("RPC", _status(rpc_ok, optional=True), rpc_detail)
    console.print(table)

    core_ok = py_ok and models_ok and serpapi_ok
    if demo:
        core_ok = (
            core_ok
            and chain_configured
            and bool(settings.contract_code_hash)
            and revision_ok
            and rpc_ok
            and wallet_funded
        )
    if core_ok:
        console.print("[green]Core face/search environment is ready.[/green]")
    else:
        console.print("[yellow]Complete the missing core items before a live run.[/yellow]")
        raise typer.Exit(code=1)


@app.command()
def scan(
    image: Path = typer.Option(..., "--image", exists=True, file_okay=True, dir_okay=False),
    i_have_consent: bool = typer.Option(False, "--i-have-consent"),
) -> None:
    """Run local face detection/encoding without searching or uploading."""
    _require_consent(i_have_consent)
    settings = _settings()
    model_results = verify_default_models(settings.model_dir)
    invalid_models = [name for name, status in model_results.items() if status != "ok"]
    if invalid_models:
        console.print("[red]Face scan failed: pinned model integrity check failed.[/red]")
        raise typer.Exit(code=2)
    backend = OpenCVFaceBackend(settings.yunet_model, settings.sface_model)
    try:
        encoding = backend.encode_one(image)
    except FaceError as exc:
        console.print(f"[red]Face scan failed:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    table = Table(title="Face scan")
    table.add_column("Check")
    table.add_column("Result")
    table.add_row("Detection", "[green]PASS — exactly one face[/green]")
    table.add_row("Confidence", f"{encoding.detection.confidence:.4f}")
    table.add_row("Embedding", f"{len(encoding.embedding)} dimensions; not persisted")
    table.add_row("Sharpness", f"{encoding.quality.sharpness:.2f}")
    table.add_row("Brightness", f"{encoding.quality.brightness:.2f}")
    table.add_row("YuNet SHA-256", encoding.models.detector_sha256)
    table.add_row("SFace SHA-256", encoding.models.recognizer_sha256)
    console.print(table)


@app.command("run")
def run_command(
    image: Path = typer.Option(..., "--image", exists=True, file_okay=True, dir_okay=False),
    live: bool = typer.Option(
        False,
        "--live",
        help="Acknowledge that this command performs a real external search.",
    ),
    i_have_consent: bool = typer.Option(False, "--i-have-consent"),
    consent_reference: str | None = typer.Option(
        None,
        "--consent-reference",
        help="Non-sensitive consent record reference; required before anchoring.",
    ),
    skip_anchor: bool = typer.Option(
        False,
        "--skip-anchor",
        help="Development only: produce evidence without a blockchain write.",
    ),
    threshold: float = typer.Option(
        0.363,
        "--threshold",
        min=-1.0,
        max=1.0,
        help="Frozen SFace cosine threshold; calibrate before final judging.",
    ),
    max_candidates: int = typer.Option(6, "--max-candidates", min=1, max=20),
    approve_post_url: str | None = typer.Option(
        None,
        "--approve-post-url",
        help="Exact permalink approved by a human; required before anchoring.",
    ),
    output_dir: Path | None = typer.Option(None, "--output-dir", file_okay=False),
) -> None:
    """Run face scan, live discovery, evidence capture, anchor, and read-back."""
    _require_consent(i_have_consent)
    settings = _settings()
    console.print(
        Panel.fit(
            "FaceProof — proof of discovery, not proof of identity or truth",
            border_style="cyan",
        )
    )
    try:
        result = run_pipeline(
            image_path=image,
            settings=settings,
            consent_acknowledged=True,
            consent_reference=consent_reference,
            live=live,
            skip_anchor=skip_anchor,
            threshold=threshold,
            max_candidates=max_candidates,
            approved_post_url=approve_post_url,
            output_dir=output_dir,
            on_stage=lambda message: console.print(f"[cyan]>[/cyan] {message}"),
        )
    except InconclusiveError as exc:
        console.print(f"[yellow]INCONCLUSIVE:[/yellow] {exc}")
        console.print(f"Partial evidence: {exc.run_dir.resolve()}")
        raise typer.Exit(code=3) from exc
    except (PipelineError, ValueError) as exc:
        console.print(f"[red]Pipeline failed:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    table = Table(title="End-to-end result")
    table.add_column("Stage")
    table.add_column("Result")
    table.add_row("Live search", f"PASS — {result.provider} / {result.search_id}")
    table.add_row("Social post", result.selected_url)
    table.add_row("Local face candidate", f"{result.local_similarity:.6f}; human review required")
    table.add_row("Manifest SHA-256", result.manifest_sha256)
    table.add_row("On-chain commitment", result.commitment)
    if result.chain_receipt:
        table.add_row(
            "Blockchain",
            f"PASS — block {result.chain_receipt.block_number}, "
            f"tx {result.chain_receipt.transaction_hash}",
        )
    else:
        table.add_row("Blockchain", "[yellow]SKIPPED — development run only[/yellow]")
    table.add_row("Evidence directory", str(result.run_dir.resolve()))
    console.print(table)
    if result.explorer_url:
        console.print(f"Explorer: [link={result.explorer_url}]{result.explorer_url}[/link]")


@app.command()
def verify(
    run_dir: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    allow_unanchored: bool = typer.Option(
        False,
        "--allow-unanchored",
        help="Development only: allow a bundle with no chain receipt.",
    ),
    expected_commitment: str | None = typer.Option(
        None,
        "--expected-commitment",
        help="Commitment copied from an independent source, such as the demo record.",
    ),
    expected_tx: str | None = typer.Option(
        None,
        "--expected-tx",
        help="Transaction hash copied from an explorer or independent demo record.",
    ),
) -> None:
    """Re-hash the evidence and read the commitment from public chain state."""
    settings = _settings()
    try:
        result = verify_run(
            run_dir,
            settings=settings,
            require_chain=not allow_unanchored,
            expected_commitment=expected_commitment,
            expected_transaction_hash=expected_tx,
        )
    except (PipelineError, ValueError, OSError) as exc:
        console.print(f"[red]Verification failed:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    table = Table(title="Independent verification")
    table.add_column("Check")
    table.add_column("Verdict")
    table.add_row("Artifact hashes", _pass_fail(result.evidence_ok))
    table.add_row("Canonical manifest sidecar", _pass_fail(result.canonical_sidecar_ok))
    table.add_row(
        "Independent commitment / tx",
        (
            "[yellow]NOT PROVIDED[/yellow]"
            if result.external_anchor_ok is None
            else _pass_fail(result.external_anchor_ok)
        ),
    )
    if result.chain is None:
        table.add_row("On-chain anchor", "[yellow]NOT CHECKED[/yellow]")
    else:
        table.add_row("RPC connected", _pass_fail(result.chain.connected))
        table.add_row("Chain ID", _pass_fail(result.chain.chain_id_matches))
        table.add_row("Registry state", _pass_fail(result.chain.anchored))
        if result.chain.confirmations_satisfied is not None:
            table.add_row(
                "Confirmations",
                f"{_pass_fail(result.chain.confirmations_satisfied)} — "
                f"{result.chain.confirmations_observed}/"
                f"{result.chain.confirmations_required}",
            )
        table.add_row(
            "Transaction receipt",
            (
                "[yellow]NOT PROVIDED[/yellow]"
                if result.chain.receipt_consistent is None
                else _pass_fail(result.chain.receipt_consistent)
            ),
        )
        table.add_row("Chain detail", result.chain.detail)
    console.print(table)
    if result.evidence_detail.get("errors"):
        for error in result.evidence_detail["errors"]:
            console.print(f"[red]•[/red] {error}")
    if result.passed:
        console.print(
            "[green bold]VERIFIED: exact evidence matches the saved commitment.[/green bold]"
        )
        console.print("Identity and truth still require human review.")
    else:
        console.print("[red bold]VERIFICATION FAILED[/red bold]")
        raise typer.Exit(code=1)


@app.command("tamper-demo")
def tamper_demo(
    run_dir: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
) -> None:
    """Modify a temporary copy and prove the verifier detects the change."""
    try:
        changed_path, result = run_tamper_demo(run_dir)
    except (PipelineError, ValueError, OSError) as exc:
        console.print(f"[red]Tamper demo failed to run:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    console.print(f"Changed one byte in a temporary copy of: {changed_path}")
    if result["ok"]:
        console.print("[red bold]UNEXPECTED PASS: tampering was not detected.[/red bold]")
        raise typer.Exit(code=1)
    console.print("[green bold]EXPECTED FAILURE: tampering was detected.[/green bold]")
    for error in result["errors"]:
        console.print(f"[green]•[/green] {error}")


def _status(ok: bool, *, optional: bool = False) -> str:
    if ok:
        return "[green]PASS[/green]"
    return "[yellow]MISSING[/yellow]" if optional else "[red]FAIL[/red]"


def _pass_fail(ok: bool) -> str:
    return "[green]PASS[/green]" if ok else "[red]FAIL[/red]"


def main() -> None:
    app()


if __name__ == "__main__":
    main()
