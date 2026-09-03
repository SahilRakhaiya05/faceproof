# Model assets

Run `uv run faceproof models download` from the repository root. The command
downloads the pinned OpenCV Zoo YuNet and SFace ONNX files and checks their
expected sizes and SHA-256 hashes before making them available to the pipeline.

The binary model files are intentionally excluded from Git. Their upstream
source and licence links are recorded in `src/faceproof/model_assets.py`.
