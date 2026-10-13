import hashlib
from pathlib import Path
from typing import Any

import pytest
from tricycle_sar.preparation.additional import require_reactiont5_assets


@pytest.fixture
def assets(tmp_path: Path) -> Any:
    weight = b"pinned checkpoint fixture"
    config = b'{"model_type": "t5"}'
    (tmp_path / "pytorch_model.bin").write_bytes(weight)
    (tmp_path / "config.json").write_bytes(config)
    spec = {
        "files": [
            {"path": "pytorch_model.bin", "sha256": hashlib.sha256(weight).hexdigest()},
            {
                "path": "config.json",
                "git_blob_sha1": hashlib.sha1(
                    b"blob " + str(len(config)).encode() + b"\0" + config
                ).hexdigest(),
            },
        ]
    }
    return tmp_path, spec


def test_requires_explicit_local_assets(assets: Any) -> None:
    _, spec = assets
    with pytest.raises(ValueError, match="local ReactionT5 assets are required"):
        require_reactiont5_assets(None, spec)


def test_requires_checkpoint_not_just_tokenizer_config(assets: Any) -> None:
    root, spec = assets
    (root / "pytorch_model.bin").unlink()
    with pytest.raises(ValueError, match="incomplete local ReactionT5 assets"):
        require_reactiont5_assets(root, spec)


def test_rejects_wrong_revision_weight(assets: Any) -> None:
    root, spec = assets
    (root / "pytorch_model.bin").write_bytes(b"different revision")
    with pytest.raises(ValueError, match="checksum mismatch"):
        require_reactiont5_assets(root, spec)


def test_requires_weight_hash(assets: Any) -> None:
    root, spec = assets
    spec["files"] = spec["files"][1:]
    with pytest.raises(ValueError, match="pinned checkpoint SHA256"):
        require_reactiont5_assets(root, spec)


def test_accepts_verified_complete_local_assets(assets: Any) -> None:
    root, spec = assets
    require_reactiont5_assets(root, spec)
