from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from tricycle_core.errors import InvalidRequestError

DEFAULT_MODEL_MANIFEST = Path(__file__).with_name("assets") / "rits-model.toml"
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequestError(f"RitS model manifest requires non-empty {key}")
    return value


@dataclass(frozen=True, slots=True)
class RitsModelAsset:
    """Pinned RitS source/checkpoint metadata kept separate from task requests."""

    model_name: str
    repository: str
    revision: str
    checkpoint_name: str
    checkpoint_path: str
    task_checkpoint_path: str
    checkpoint_source: str
    checkpoint_sha256: str | None = None
    training_entrypoint: str = "scripts/train.py"
    inference_entrypoint: str = "scripts/sample_transition_state.py"
    finetune_protocol: str = "finetune_from"
    resume_protocol: str = "resume"

    def __post_init__(self) -> None:
        for field_name in (
            "model_name",
            "repository",
            "revision",
            "checkpoint_name",
            "checkpoint_path",
            "task_checkpoint_path",
            "checkpoint_source",
            "training_entrypoint",
            "inference_entrypoint",
            "finetune_protocol",
            "resume_protocol",
        ):
            if not getattr(self, field_name).strip():
                raise InvalidRequestError(f"RitS model manifest {field_name} must not be empty")
        if not _COMMIT_PATTERN.fullmatch(self.revision):
            raise InvalidRequestError("RitS model revision must be a 40-character git commit")
        if self.checkpoint_sha256 is not None and not _SHA256_PATTERN.fullmatch(
            self.checkpoint_sha256
        ):
            raise InvalidRequestError("checkpoint_sha256 must be a 64-character hex digest")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> RitsModelAsset:
        checksum = payload.get("checkpoint_sha256")
        if checksum is not None and not isinstance(checksum, str):
            raise InvalidRequestError("checkpoint_sha256 must be a string when provided")
        return cls(
            model_name=_required_text(payload, "model_name"),
            repository=_required_text(payload, "repository"),
            revision=_required_text(payload, "revision"),
            checkpoint_name=_required_text(payload, "checkpoint_name"),
            checkpoint_path=_required_text(payload, "checkpoint_path"),
            task_checkpoint_path=_required_text(payload, "task_checkpoint_path"),
            checkpoint_source=_required_text(payload, "checkpoint_source"),
            checkpoint_sha256=checksum.lower() if checksum else None,
            training_entrypoint=str(payload.get("training_entrypoint", "scripts/train.py")),
            inference_entrypoint=str(
                payload.get("inference_entrypoint", "scripts/sample_transition_state.py")
            ),
            finetune_protocol=str(payload.get("finetune_protocol", "finetune_from")),
            resume_protocol=str(payload.get("resume_protocol", "resume")),
        )

    def resolve_checkpoint(self, root: str | Path = ".") -> Path:
        """Return the expected local checkpoint path relative to ``root``."""

        path = Path(self.checkpoint_path)
        return path if path.is_absolute() else Path(root) / path

    def require_checkpoint(self, root: str | Path = ".") -> Path:
        """Require the checkpoint and, when recorded, verify its SHA-256 digest."""

        path = self.resolve_checkpoint(root)
        if not path.is_file():
            raise InvalidRequestError(
                f"RitS checkpoint is missing: place {self.checkpoint_name} at {path}"
            )
        if self.checkpoint_sha256:
            digest = sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            actual = digest.hexdigest()
            if actual != self.checkpoint_sha256:
                raise InvalidRequestError(
                    f"RitS checkpoint checksum mismatch for {path}: "
                    f"expected {self.checkpoint_sha256}, got {actual}"
                )
        return path


def load_model_asset(manifest: str | Path = DEFAULT_MODEL_MANIFEST) -> RitsModelAsset:
    """Load a pinned RitS model manifest without accessing the network."""

    manifest_path = Path(manifest)
    try:
        with manifest_path.open("rb") as handle:
            document = tomllib.load(handle)
    except OSError as exc:
        raise InvalidRequestError(f"cannot read RitS model manifest: {manifest_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise InvalidRequestError(f"invalid RitS model manifest: {manifest_path}") from exc

    payload = document.get("model", document)
    if not isinstance(payload, Mapping):
        raise InvalidRequestError("RitS model manifest must contain a [model] table")
    return RitsModelAsset.from_mapping(payload)


def load_default_model_asset() -> RitsModelAsset:
    """Load the component's pinned default RitS checkpoint metadata."""

    return load_model_asset()
