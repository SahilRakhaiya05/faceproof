from faceproof.model_assets import DEFAULT_MODELS


def test_default_model_specs_are_pinned() -> None:
    assert {model.filename for model in DEFAULT_MODELS} == {
        "face_detection_yunet_2023mar.onnx",
        "face_recognition_sface_2021dec.onnx",
    }
    for model in DEFAULT_MODELS:
        assert len(model.sha256) == 64
        assert model.byte_size > 0
        assert model.url.startswith("https://media.githubusercontent.com/")
