"""Build a structural topology from a rps_solver input JSON.

Two directed graphs are produced:
  - material_to_block: material code -> list[(block_code, role='consumer')]
  - block_to_material: block_code  -> list[(material_code, role='input'/'output')]
plus convenience metadata: yield ratios, prices, capacity bounds.
"""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


BLOCK_SECTIONS = (
    "cduBlock", "manuBlock", "blenderBlock", "mixerBlock",
    "spliterBlock", "customBlock", "processBlock",
)


@dataclass
class MaterialInfo:
    code: str
    type: str = ""
    sell_price: float | None = None
    sell_lb: float | None = None
    sell_ub: float | None = None
    purchase_price: float | None = None
    purchase_lb: float | None = None
    purchase_ub: float | None = None
    material_force_balance: bool = False
    input_blocks: list = field(default_factory=list)
    output_blocks: list = field(default_factory=list)
    properties: dict = field(default_factory=dict)   # code -> start


@dataclass
class BlockInfo:
    code: str
    section: str
    type: str = ""
    inputs: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    cases: list = field(default_factory=list)            # for manuBlock
    mass_flow_capacity: list = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)  # original dict


@dataclass
class Topology:
    materials: dict = field(default_factory=dict)     # code -> MaterialInfo
    blocks: dict = field(default_factory=dict)        # code -> BlockInfo
    consumers_of: dict = field(default_factory=lambda: defaultdict(list))  # material -> [block_code]
    producers_of: dict = field(default_factory=lambda: defaultdict(list))  # material -> [block_code]

    def get_block(self, code: str) -> BlockInfo | None:
        return self.blocks.get(code)

    def get_material(self, code: str) -> MaterialInfo | None:
        return self.materials.get(code)


def _safe_dict(x):
    return x if isinstance(x, dict) else {}


def _io_codes(io_list):
    return [
        (x.get("code") if isinstance(x, dict) else x)
        for x in (io_list or [])
        if x is not None
    ]


def build_topology(input_json: dict) -> Topology:
    """Parse the rps_solver input JSON into a Topology object."""
    topo = Topology()

    # Materials
    for m in input_json.get("materialList", []) or []:
        sell = _safe_dict(m.get("sell"))
        purc = _safe_dict(m.get("purchase"))
        info = MaterialInfo(
            code = m.get("code", ""),
            type = m.get("type", ""),
            sell_price = sell.get("price"),
            sell_lb    = sell.get("lb"),
            sell_ub    = sell.get("ub"),
            purchase_price = purc.get("price"),
            purchase_lb    = purc.get("lb"),
            purchase_ub    = purc.get("ub"),
            material_force_balance = bool(m.get("material_force_balance", False)),
            input_blocks  = list(m.get("input")  or []),
            output_blocks = list(m.get("output") or []),
            properties = {
                p.get("code"): p.get("start")
                for p in (m.get("property") or [])
                if isinstance(p, dict) and p.get("code")
            },
        )
        topo.materials[info.code] = info

    # Blocks
    for section in BLOCK_SECTIONS:
        for b in input_json.get(section, []) or []:
            ins  = _io_codes(b.get("input_material"))
            outs = _io_codes(b.get("output_material"))
            info = BlockInfo(
                code   = b.get("code", ""),
                section = section,
                type   = b.get("type", ""),
                inputs = ins,
                outputs = outs,
                cases  = b.get("cases", []) or [],
                mass_flow_capacity = b.get("mass_flow_capacity", []) or [],
                raw    = b,
            )
            topo.blocks[info.code] = info
            for m in ins:
                topo.consumers_of[m].append(info.code)
            for m in outs:
                topo.producers_of[m].append(info.code)

    return topo
