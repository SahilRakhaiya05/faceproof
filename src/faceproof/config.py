from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _optional_env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration loaded from environment variables.

    Secrets are intentionally never represented in ``repr`` output.
    """

    serpapi_api_key: str | None = field(repr=False)
    model_dir: Path
    output_dir: Path
    rpc_url: str
    chain_id: int
    contract_address: str | None
    private_key: str | None = field(repr=False)
    confirmations: int
    http_timeout_seconds: float
    contract_code_hash: str | None = None
    source_revision: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            serpapi_api_key=_optional_env("SERPAPI_API_KEY"),
            model_dir=Path(os.getenv("FACEPROOF_MODEL_DIR", "models")),
            output_dir=Path(os.getenv("FACEPROOF_OUTPUT_DIR", "evidence")),
            rpc_url=os.getenv("FACEPROOF_RPC_URL", "https://sepolia.base.org").strip(),
            chain_id=int(os.getenv("FACEPROOF_CHAIN_ID", "84532")),
            contract_address=_optional_env("FACEPROOF_CONTRACT_ADDRESS"),
            private_key=_optional_env("FACEPROOF_PRIVATE_KEY"),
            confirmations=max(1, int(os.getenv("FACEPROOF_CONFIRMATIONS", "1"))),
            http_timeout_seconds=float(os.getenv("FACEPROOF_HTTP_TIMEOUT_SECONDS", "30")),
            contract_code_hash=_optional_env("FACEPROOF_CONTRACT_CODE_HASH"),
            source_revision=_optional_env("FACEPROOF_SOURCE_REVISION"),
        )

    @property
    def yunet_model(self) -> Path:
        return self.model_dir / "face_detection_yunet_2023mar.onnx"

    @property
    def sface_model(self) -> Path:
        return self.model_dir / "face_recognition_sface_2021dec.onnx"

    def require_serpapi_key(self) -> str:
        if not self.serpapi_api_key:
            raise ValueError("SERPAPI_API_KEY is required for live Google Lens search")
        return self.serpapi_api_key

    def require_chain_write(self) -> tuple[str, str]:
        if not self.contract_address:
            raise ValueError("FACEPROOF_CONTRACT_ADDRESS is required to anchor evidence")
        if not self.private_key:
            raise ValueError("FACEPROOF_PRIVATE_KEY is required to anchor evidence")
        return self.contract_address, self.private_key
