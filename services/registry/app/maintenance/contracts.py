"""Loader for the desired-state contract registry (``desired_state.json``).

The registry is trusted evaluation criteria. Everything a repair may NOT
touch together with the product it repairs is derived from it here
(``evaluation_paths``), so adding a detector or a verification test to a
contract automatically protects it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

REGISTRY_PATH = Path(__file__).with_name("desired_state.json")
#: The registry's repository-relative path (images do not keep the repo layout).
REGISTRY_REPO_PATH = "services/registry/app/maintenance/desired_state.json"

#: The Maintenance OS itself and its governing documents. A candidate that
#: edits these is constitutional self-modification: RED, never auto-released.
KERNEL_PATHS: Tuple[str, ...] = (
    "services/registry/app/maintenance/**",
    "services/registry/app/society/risk.py",
    "services/registry/app/society/policy.py",
    "services/registry/app/society/fitness.py",
    "services/registry/app/society/promotion.py",
    "services/registry/app/society/promotion_github.py",
    "services/registry/app/society/surface.py",
    "services/registry/app/society/public_surface_contract.json",
    "tests/society/maintenance/**",
    "services/registry/app/api/routes/maintenance.py",
    "deploy/maintenance/**",
    "docs/adr/0010-autonomous-maintenance-os.md",
    "docs/MAINTENANCE_POLICY.md",
    "docs/MAINTENANCE_RELEASE.md",
    "docs/MAINTENANCE_STATE_MACHINE.md",
    ".railway/**",
)


@dataclass(frozen=True)
class DesiredStateContract:
    id: str
    description: str
    source_of_truth: str
    detectors: Tuple[str, ...]
    verification_tests: Tuple[str, ...]
    sli: Optional[str] = None
    item_sli: Dict[str, str] = field(default_factory=dict)
    rules: Tuple[str, ...] = ()
    budgets: Dict[str, int] = field(default_factory=dict)
    autonomous_repair: bool = True

    def sli_for_item(self, item: str) -> str:
        if item in self.item_sli:
            return self.item_sli[item]
        return self.item_sli.get("*") or self.sli or self.id


@dataclass(frozen=True)
class Registry:
    version: int
    contracts: Tuple[DesiredStateContract, ...]

    def get(self, contract_id: str) -> DesiredStateContract:
        for c in self.contracts:
            if c.id == contract_id:
                return c
        raise KeyError(contract_id)

    def evaluation_paths(self) -> FrozenSet[str]:
        """Every trusted evaluation artefact: detectors, verification tests,
        sources of truth, the registry itself and the kernel."""
        out = {REGISTRY_REPO_PATH}
        for c in self.contracts:
            out.update(c.detectors)
            out.update(c.verification_tests)
            if c.source_of_truth.endswith((".json", ".py")):
                out.add(c.source_of_truth)
        out.update(KERNEL_PATHS)
        return frozenset(p for p in out if p)


@lru_cache(maxsize=1)
def load_registry(path: str = str(REGISTRY_PATH)) -> Registry:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    contracts: List[DesiredStateContract] = []
    seen = set()
    for c in raw["contracts"]:
        if c["id"] in seen:
            raise ValueError(f"duplicate contract id {c['id']!r}")
        seen.add(c["id"])
        contracts.append(
            DesiredStateContract(
                id=c["id"],
                description=c["description"],
                source_of_truth=c["source_of_truth"],
                detectors=tuple(c.get("detectors") or ()),
                verification_tests=tuple(c.get("verification_tests") or ()),
                sli=c.get("sli"),
                item_sli=dict(c.get("item_sli") or {}),
                rules=tuple(c.get("rules") or ()),
                budgets={k: int(v) for k, v in (c.get("budgets") or {}).items()},
                autonomous_repair=bool(c.get("autonomous_repair", True)),
            )
        )
    return Registry(version=int(raw["version"]), contracts=tuple(contracts))


__all__ = ["DesiredStateContract", "Registry", "load_registry", "KERNEL_PATHS", "REGISTRY_PATH"]
