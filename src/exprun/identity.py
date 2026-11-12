"""A run's identity: its config hash, the content of its inputs, its code version and fingerprint."""

from __future__ import annotations

from dataclasses import dataclass

from exprun.codeversion import code_version
from exprun.config import Config
from exprun.hashing import fingerprint, hash_input, hash_value


@dataclass(frozen=True)
class Identity:
    spec: dict | None            # task, seed and params; None until a missing seed is drawn
    config_hash: str | None
    inputs: dict[str, dict]      # name -> {path, kind, sha256, bytes, files}
    code: dict
    fingerprint: str | None

    def to_dict(self) -> dict:
        return {"spec": self.spec, "config_hash": self.config_hash, "inputs": self.inputs, "code": self.code,
                "fingerprint": self.fingerprint}


def identify(config: Config, seed: int | None = None) -> Identity:
    """Hash everything that identifies a run. Without a seed (none in the config, none drawn yet),
    the config hash and fingerprint are left unset, since they depend on it."""
    inputs = {}
    for name, path in config.inputs.items():
        inputs[name] = {"path": config.input_paths[name], **hash_input(path)}
    code = code_version(config.module, config.function, config.folder)
    if config.seed is None and seed is None:
        return Identity(None, None, inputs, code, None)
    spec = config.spec(seed)
    config_hash = hash_value(spec)
    return Identity(spec, config_hash, inputs, code, fingerprint(config_hash, inputs, code))
