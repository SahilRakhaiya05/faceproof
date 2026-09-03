from __future__ import annotations

import json
import os
import re
import shutil
import socket

# Only fixed-argument local tool execution is permitted in this module.
import subprocess  # nosec B404
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from web3 import Account, Web3
from web3.types import TxReceipt

from .config import Settings
from .provenance import ProvenanceError, verify_git_source_revision

LOCAL_CHAIN_ID = 31_337
LOCAL_HOST = "127.0.0.1"
STANDARD_ACCOUNT_PATH = "m/44'/60'/0'/0/0"
_TOOL_ENV = {
    "forge": "FACEPROOF_FORGE_PATH",
    "anvil": "FACEPROOF_ANVIL_PATH",
}


class LocalDemoError(RuntimeError):
    """Raised when the disposable localhost blockchain cannot be prepared safely."""


class _Process(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


@dataclass(frozen=True, slots=True)
class LocalDemoRuntime:
    """Non-secret facts about one disposable chain-backed web session."""

    settings: Settings
    web_url: str
    rpc_url: str
    contract_address: str
    contract_code_hash: str
    chain_id: int
    source_revision: str


def find_foundry_tool(
    name: str,
    *,
    repository_root: Path,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Locate a Foundry binary via explicit override, PATH, or bundled versions."""

    tool = name.casefold().strip()
    if tool not in _TOOL_ENV:
        raise ValueError(f"Unsupported Foundry tool: {name}")
    environment = os.environ if environ is None else environ
    override = environment.get(_TOOL_ENV[tool], "").strip()
    if override:
        candidate = Path(override).expanduser().resolve()
        if not candidate.is_file():
            raise LocalDemoError(
                f"{_TOOL_ENV[tool]} does not point to an existing {tool} executable"
            )
        return candidate

    path_match = shutil.which(tool)
    if path_match:
        return Path(path_match).resolve()

    executable_names = {tool, f"{tool}.exe"}
    foundry_root = Path(repository_root).resolve() / ".tools" / "foundry"
    candidates = [
        path.resolve()
        for path in foundry_root.rglob("*")
        if path.is_file() and path.name.casefold() in executable_names
    ]
    if candidates:
        return max(candidates, key=_foundry_version_key)

    env_name = _TOOL_ENV[tool]
    raise LocalDemoError(
        f"Foundry {tool} was not found. Install Foundry, add it to PATH, or set {env_name}."
    )


@contextmanager
def local_demo_runtime(
    base_settings: Settings,
    *,
    web_port: int = 8787,
    repository_root: Path | None = None,
    chain_port: int | None = None,
    on_status: Callable[[str], None] | None = None,
) -> Iterator[LocalDemoRuntime]:
    """Prepare a fresh localhost chain and always stop it when the session ends.

    The mnemonic and derived private key exist only in process memory. Neither is
    written to ``.env`` or any evidence/configuration file by this launcher.
    """

    _validate_port(web_port, "Web port")
    if not _port_available(web_port):
        raise LocalDemoError(f"Web port {web_port} is already in use on {LOCAL_HOST}")
    root = _repository_root(repository_root)
    forge = find_foundry_tool("forge", repository_root=root)
    anvil = find_foundry_tool("anvil", repository_root=root)
    revision = _current_clean_revision(root)
    _notify(on_status, "Compiling the pinned EvidenceRegistry contract locally…")
    artifact = _compile_registry(forge, root)
    selected_chain_port = chain_port or _available_port()
    _validate_port(selected_chain_port, "Anvil port")
    if not _port_available(selected_chain_port):
        raise LocalDemoError(f"Anvil port {selected_chain_port} is already in use on {LOCAL_HOST}")

    mnemonic, account = _new_ephemeral_account()
    process: _Process | None = None
    try:
        _notify(on_status, "Starting a disposable localhost blockchain…")
        process = _start_anvil(
            anvil,
            port=selected_chain_port,
            mnemonic=mnemonic,
        )
        # The phrase is no longer needed by this process after Anvil has started.
        mnemonic = ""
        rpc_url = f"http://{LOCAL_HOST}:{selected_chain_port}"
        web3 = _wait_for_anvil(process, rpc_url, account.address)
        _notify(on_status, "Deploying and pinning the local registry bytecode…")
        contract_address, code_hash = _deploy_registry(web3, artifact, account.address)
        runtime_settings = replace(
            base_settings,
            rpc_url=rpc_url,
            chain_id=LOCAL_CHAIN_ID,
            contract_address=contract_address,
            private_key=Web3.to_hex(account.key),
            confirmations=1,
            contract_code_hash=code_hash,
            source_revision=revision,
        )
        yield LocalDemoRuntime(
            settings=runtime_settings,
            web_url=f"http://{LOCAL_HOST}:{web_port}",
            rpc_url=rpc_url,
            contract_address=contract_address,
            contract_code_hash=code_hash,
            chain_id=LOCAL_CHAIN_ID,
            source_revision=revision,
        )
    except LocalDemoError:
        raise
    except Exception as exc:
        raise LocalDemoError(
            f"The disposable local chain could not be prepared ({type(exc).__name__})."
        ) from exc
    finally:
        mnemonic = ""
        if process is not None:
            _stop_anvil(process)


def serve_local_demo(
    base_settings: Settings,
    *,
    port: int = 8787,
    open_browser: bool = True,
    on_status: Callable[[str], None] | None = None,
) -> None:
    """Serve the existing console against an in-memory, disposable chain config."""

    # Imported lazily so helper/unit tests do not initialize the ASGI server.
    import uvicorn

    from .web import create_app

    browser_timer: threading.Timer | None = None
    with local_demo_runtime(
        base_settings,
        web_port=port,
        on_status=on_status,
    ) as runtime:
        _notify(on_status, f"Local judge console ready: {runtime.web_url}")
        _notify(
            on_status,
            "EPHEMERAL LOCAL CHAIN: records disappear when this command stops; "
            "no real funds are required; this is not public-chain proof.",
        )
        if open_browser:
            browser_timer = threading.Timer(0.8, webbrowser.open, args=(runtime.web_url,))
            browser_timer.daemon = True
            browser_timer.start()
        try:
            uvicorn.run(
                create_app(runtime.settings),
                host=LOCAL_HOST,
                port=port,
                log_level="warning",
            )
        finally:
            if browser_timer is not None:
                browser_timer.cancel()


def _repository_root(explicit: Path | None) -> Path:
    if explicit is not None:
        root = Path(explicit).resolve()
        if (root / "contracts" / "foundry.toml").is_file():
            return root
        raise LocalDemoError("Repository root does not contain contracts/foundry.toml")

    for parent in Path(__file__).resolve().parents:
        if (parent / "contracts" / "foundry.toml").is_file() and ((parent / ".git").exists()):
            return parent
    raise LocalDemoError(
        "Run local-demo from a FaceProof Git checkout containing the contracts directory."
    )


def _compile_registry(forge: Path, repository_root: Path) -> Path:
    contract_root = repository_root / "contracts"
    try:
        # The executable is explicitly resolved and no shell is used.
        completed = subprocess.run(  # nosec B603
            [str(forge), "build", "--root", str(contract_root)],
            cwd=repository_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LocalDemoError("Foundry could not compile EvidenceRegistry.") from exc
    if completed.returncode != 0:
        raise LocalDemoError(
            "Foundry could not compile EvidenceRegistry; run `forge build` in contracts "
            "to inspect the compiler output."
        )
    artifact = contract_root / "out" / "EvidenceRegistry.sol" / "EvidenceRegistry.json"
    if not artifact.is_file():
        raise LocalDemoError("Foundry completed but the EvidenceRegistry artifact is missing")
    return artifact


def _current_clean_revision(repository_root: Path) -> str:
    git = shutil.which("git")
    if git is None:
        raise LocalDemoError("Git is required to bind the demo to reviewed source code")
    try:
        # Git receives fixed arguments and no shell is used.
        completed = subprocess.run(  # nosec B603
            [git, "-C", str(repository_root), "rev-parse", "--verify", "HEAD"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LocalDemoError("Could not resolve the current Git revision") from exc
    revision = completed.stdout.strip().lower()
    if completed.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise LocalDemoError("Could not resolve the current full Git revision")
    try:
        verified = verify_git_source_revision(
            revision,
            cwd=repository_root,
            source_file=Path(__file__),
            require_clean=True,
        )
    except ProvenanceError as exc:
        raise LocalDemoError(
            "Local blockchain writes require a clean Git checkout. Commit or stash changes "
            "and launch local-demo again."
        ) from exc
    return verified.revision


def _new_ephemeral_account():
    # eth-account deliberately gates HD helpers as unaudited. They are appropriate here
    # only because the generated wallet is random, localhost-only, and immediately discarded.
    Account.enable_unaudited_hdwallet_features()
    _generated, mnemonic = Account.create_with_mnemonic(
        num_words=12,
        account_path=STANDARD_ACCOUNT_PATH,
    )
    account = Account.from_mnemonic(mnemonic, account_path=STANDARD_ACCOUNT_PATH)
    return mnemonic, account


def _start_anvil(anvil: Path, *, port: int, mnemonic: str) -> _Process:
    command = [
        str(anvil),
        "--host",
        LOCAL_HOST,
        "--port",
        str(port),
        "--chain-id",
        str(LOCAL_CHAIN_ID),
        "--accounts",
        "1",
        "--balance",
        "1000",
        "--mnemonic",
        mnemonic,
        "--silent",
    ]
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        # The executable is explicitly resolved and no shell is used.
        return subprocess.Popen(  # nosec B603
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            creationflags=creation_flags,
        )
    except OSError as exc:
        raise LocalDemoError("Anvil could not be started") from exc


def _wait_for_anvil(
    process: _Process,
    rpc_url: str,
    expected_account: str,
    *,
    timeout_seconds: float = 15,
) -> Web3:
    web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 1}))
    deadline = time.monotonic() + timeout_seconds
    last_error_type: str | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LocalDemoError("Anvil exited before its localhost RPC became ready")
        try:
            connected = web3.is_connected()
            chain_matches = connected and int(web3.eth.chain_id) == LOCAL_CHAIN_ID
            accounts = list(web3.eth.accounts) if chain_matches else []
            if accounts and Web3.to_checksum_address(accounts[0]) == Web3.to_checksum_address(
                expected_account
            ):
                return web3
        except Exception as exc:
            # RPC startup commonly races the first few connection attempts. Retain
            # only the exception class; response bodies and URLs are not disclosed.
            last_error_type = type(exc).__name__
        time.sleep(0.1)
    detail = f" ({last_error_type})" if last_error_type else ""
    raise LocalDemoError(f"Timed out waiting for the localhost Anvil RPC{detail}")


def _deploy_registry(web3: Web3, artifact: Path, deployer: str) -> tuple[str, str]:
    try:
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        abi = payload["abi"]
        bytecode = payload["bytecode"]["object"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise LocalDemoError("The compiled EvidenceRegistry artifact is malformed") from exc
    if not isinstance(abi, list) or not isinstance(bytecode, str) or not bytecode.startswith("0x"):
        raise LocalDemoError("The compiled EvidenceRegistry artifact is malformed")
    try:
        factory = web3.eth.contract(abi=abi, bytecode=bytecode)
        transaction_hash = factory.constructor().transact(
            {"from": Web3.to_checksum_address(deployer)}
        )
        receipt: TxReceipt = web3.eth.wait_for_transaction_receipt(
            transaction_hash,
            timeout=30,
        )
        if int(receipt["status"]) != 1 or receipt["contractAddress"] is None:
            raise LocalDemoError("The local registry deployment transaction failed")
        address = Web3.to_checksum_address(receipt["contractAddress"])
        runtime_code = bytes(web3.eth.get_code(address))
    except LocalDemoError:
        raise
    except Exception as exc:
        raise LocalDemoError("Could not deploy EvidenceRegistry to the local chain") from exc
    if not runtime_code:
        raise LocalDemoError("The deployed local registry has no runtime bytecode")
    return address, Web3.to_hex(Web3.keccak(runtime_code))


def _stop_anvil(process: _Process) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            # Process cleanup is best-effort after both graceful and forced shutdown.
            pass


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((LOCAL_HOST, 0))
        return int(listener.getsockname()[1])


def _port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((LOCAL_HOST, port))
    except OSError:
        return False
    return True


def _validate_port(port: int, label: str) -> None:
    if type(port) is not int or not 1024 <= port <= 65_535:
        raise LocalDemoError(f"{label} must be between 1024 and 65535")


def _foundry_version_key(path: Path) -> tuple[tuple[int, object], ...]:
    text = "/".join(path.parts[-3:]).casefold()
    return tuple(
        (0, int(token)) if token.isdigit() else (1, token) for token in re.split(r"(\d+)", text)
    )


def _notify(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is not None:
        callback(message)
