from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tricycle_core.errors import InvalidRequestError

DEFAULT_MODEL_MANIFEST = Path(__file__).with_name("assets") / "rxnresid-model.toml"
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequestError(f"RxnResid model manifest requires non-empty {key}")
    return value.strip()


@dataclass(frozen=True, slots=True)
class RxnResidModelAsset:
    """Pinned RxnResid source and task-entrypoint metadata."""

    model_name: str
    repository: str
    revision: str
    config_path: str
    training_entrypoint: str
    inference_entrypoint: str
    default_data_path: str
    finetune_protocol: str = "refit"

    def __post_init__(self) -> None:
        for field_name in (
            "model_name",
            "repository",
            "revision",
            "config_path",
            "training_entrypoint",
            "inference_entrypoint",
            "default_data_path",
            "finetune_protocol",
        ):
            value = getattr(self, field_name)
            if not value.strip():
                raise InvalidRequestError(f"RxnResid model manifest {field_name} must not be empty")
        if not _COMMIT_PATTERN.fullmatch(self.revision):
            raise InvalidRequestError("RxnResid model revision must be a 40-character git commit")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> RxnResidModelAsset:
        return cls(
            model_name=_required_text(payload, "model_name"),
            repository=_required_text(payload, "repository"),
            revision=_required_text(payload, "revision"),
            config_path=_required_text(payload, "config_path"),
            training_entrypoint=_required_text(payload, "training_entrypoint"),
            inference_entrypoint=_required_text(payload, "inference_entrypoint"),
            default_data_path=_required_text(payload, "default_data_path"),
            finetune_protocol=_required_text(
                payload,
                "finetune_protocol",
            )
            if payload.get("finetune_protocol") is not None
            else "refit",
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "model_name": self.model_name,
            "repository": self.repository,
            "revision": self.revision,
            "config_path": self.config_path,
            "training_entrypoint": self.training_entrypoint,
            "inference_entrypoint": self.inference_entrypoint,
            "default_data_path": self.default_data_path,
            "finetune_protocol": self.finetune_protocol,
        }


def load_model_asset(manifest: str | Path = DEFAULT_MODEL_MANIFEST) -> RxnResidModelAsset:
    """Load a pinned RxnResid manifest without accessing the network."""

    manifest_path = Path(manifest)
    try:
        with manifest_path.open("rb") as handle:
            document = tomllib.load(handle)
    except OSError as exc:
        raise InvalidRequestError(f"cannot read RxnResid model manifest: {manifest_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise InvalidRequestError(f"invalid RxnResid model manifest: {manifest_path}") from exc

    payload = document.get("model", document)
    if not isinstance(payload, Mapping):
        raise InvalidRequestError("RxnResid model manifest must contain a [model] table")
    return RxnResidModelAsset.from_mapping(payload)


def load_default_model_asset() -> RxnResidModelAsset:
    """Load the component's pinned default RxnResid source metadata."""

    return load_model_asset()
