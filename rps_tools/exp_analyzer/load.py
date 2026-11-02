"""Load and validate the experiment files."""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REQUIRED_PATCH_KEYS = ("experiment_name", "modifications", "analysis", "files")
REQUIRED_FILE_KEYS = ("base_input", "base_output", "modified_input", "modified_output")


@dataclass
class ExperimentContext:
    exp_dir: Path
    patch: dict
    base_input: dict
    base_output: dict
    mod_input: dict
    mod_output: dict
    name: str = ""
    reports: list = field(default_factory=list)

    @property
    def entry_point(self) -> dict:
        return self.patch.get("analysis", {}).get("entry_point", {})

    @property
    def bfs_max_depth(self) -> int:
        return int(self.patch.get("analysis", {}).get("bfs_max_depth", 12))

    @property
    def min_flow_threshold(self) -> float:
        return float(self.patch.get("analysis", {}).get("min_flow_threshold", 1e-6))

    @property
    def critical_path_threshold_pct(self) -> float:
        return float(self.patch.get("analysis", {}).get("critical_path_threshold_pct", 1.0))

    @property
    def hub_cutoff_producers(self) -> int:
        return int(self.patch.get("analysis", {}).get("hub_cutoff_producers", 8))

    @property
    def hub_cutoff_consumers(self) -> int:
        return int(self.patch.get("analysis", {}).get("hub_cutoff_consumers", 8))

    @property
    def delta_chain_min_abs(self) -> float:
        return float(self.patch.get("analysis", {}).get("delta_chain_min_abs", 1.0))

    @property
    def delta_chain_min_rel(self) -> float:
        return float(self.patch.get("analysis", {}).get("delta_chain_min_rel", 0.5))

    @property
    def delta_chain_require_state_change(self) -> bool:
        return bool(self.patch.get("analysis", {}).get("delta_chain_require_state_change", True))


def _read_json(p: Path) -> dict:
    with open(p, "r", encoding="utf-8") as fp:
        return json.load(fp)


def _validate_patch(patch: dict) -> None:
    missing = [k for k in REQUIRED_PATCH_KEYS if k not in patch]
    if missing:
        raise ValueError(f"patch.json missing required keys: {missing}")
    files = patch.get("files", {})
    miss_files = [k for k in REQUIRED_FILE_KEYS if k not in files]
    if miss_files:
        raise ValueError(f"patch.json 'files' missing: {miss_files}")
    ep = patch.get("analysis", {}).get("entry_point") or {}
    if not ep.get("type") or not ep.get("code"):
        raise ValueError("patch.json 'analysis.entry_point' must have 'type' and 'code'")


def load_experiment(exp_dir: str | Path) -> ExperimentContext:
    """Load patch.json plus the four referenced input/output JSONs."""
    exp_dir = Path(exp_dir).resolve()
    patch_path = exp_dir / "patch.json"
    if not patch_path.is_file():
        raise FileNotFoundError(f"patch.json not found in {exp_dir}")

    patch = _read_json(patch_path)
    _validate_patch(patch)

    files = patch["files"]
    def _resolve(name: str) -> Path:
        # accept absolute path or relative to exp_dir
        p = Path(files[name])
        if not p.is_absolute():
            p = exp_dir / p
        if not p.is_file():
            raise FileNotFoundError(f"{name} not found: {p}")
        return p

    base_input  = _read_json(_resolve("base_input"))
    base_output = _read_json(_resolve("base_output"))
    mod_input   = _read_json(_resolve("modified_input"))
    mod_output  = _read_json(_resolve("modified_output"))

    return ExperimentContext(
        exp_dir   = exp_dir,
        patch     = patch,
        base_input  = base_input,
        base_output = base_output,
        mod_input   = mod_input,
        mod_output  = mod_output,
        name        = patch["experiment_name"],
        reports     = list(patch.get("analysis", {}).get("reports") or []),
    )
