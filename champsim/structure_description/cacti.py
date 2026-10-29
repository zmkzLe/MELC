from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from structure_description.validate import MirrorSets, Sampler, StructureDescription, StateField, Table, load
from structure_description.vocabulary import SampledSets, Scope, set_sample_rate

DEFAULT_SETS = 2048
DEFAULT_WAYS = 16
DEFAULT_CORES = 1


@dataclass(frozen=True)
class Array:
    name: str
    depth: int
    width_bits: int
    declared_width: int
    entries: int
    origin: str

    @property
    def total_bits(self) -> int:
        return self.depth * self.width_bits

    def sram_spec_kwargs(self) -> dict:
        return {
            "name": self.name,
            "depth": self.depth,
            "width": self.width_bits,
            "ports": "rw",
            "mask_gran": None,
            "num_rw_ports": 1,
            "num_read_ports": 0,
            "num_write_ports": 0,
        }


@dataclass(frozen=True)
class Register:
    name: str
    width_bits: int
    instances: int
    origin: str

    @property
    def total_bits(self) -> int:
        return self.width_bits * self.instances


@dataclass(frozen=True)
class Budget:
    arrays: tuple[Array, ...]
    registers: tuple[Register, ...]
    sets: int
    ways: int
    cores: int

    @property
    def sram_bits(self) -> int:
        return sum(a.total_bits for a in self.arrays)

    @property
    def register_bits(self) -> int:
        return sum(r.total_bits for r in self.registers)

    @property
    def total_bits(self) -> int:
        return self.sram_bits + self.register_bits

    @property
    def total_bytes(self) -> float:
        return self.total_bits / 8

    def array_bits_per_line(self) -> float:
        return self.sram_bits / (self.sets * self.ways)

    def bits_per_line(self) -> float:
        return self.total_bits / (self.sets * self.ways)


def _state_entry(
    f: StateField, sets: int, ways: int, cores: int
) -> Array | Register:
    if f.scope is Scope.PER_LINE:
        return Array(
            name=f.name,
            depth=sets,
            width_bits=f.width * ways,
            declared_width=f.width,
            entries=sets * ways,
            origin="per_line",
        )
    if f.scope is Scope.PER_SET:
        return Array(
            name=f.name,
            depth=sets,
            width_bits=f.width,
            declared_width=f.width,
            entries=sets,
            origin="per_set",
        )
    if f.scope is Scope.PER_CPU:
        return Register(
            name=f.name, width_bits=f.width, instances=cores, origin="per_cpu"
        )
    if f.scope is Scope.GLOBAL:
        return Register(name=f.name, width_bits=f.width, instances=1, origin="global")
    raise AssertionError(f"unhandled scope {f.scope!r} for field {f.name!r}")


def _table_entry(t: Table, cores: int) -> Array:
    rows = t.entries * (cores if t.scope is Scope.PER_CPU else 1)
    width = t.width + (1 if t.unset else 0)
    return Array(
        name=t.name,
        depth=rows,
        width_bits=width,
        declared_width=width,
        entries=rows,
        origin="table",
    )


def sampled_sets(s: Sampler, sets: int) -> int:
    if isinstance(s.sets, MirrorSets):
        if s.sets.mirror > sets:
            raise ValueError(f"sampler {s.name!r} mirrors {s.sets.mirror} sets, more than the {sets} the cache has")
        return s.sets.mirror
    if s.sets is SampledSets.CATEGORY_ZERO:
        return sets // set_sample_rate(sets)
    raise AssertionError(f"unhandled sampled sets {s.sets!r} for sampler {s.name!r}")


def _sampler_entry(s: Sampler, sets: int, cores: int) -> Array:
    rows = sampled_sets(s, sets) * (1 << s.extra_index_bits) * (cores if s.scope is Scope.PER_CPU else 1)
    entry_bits = 1 + s.tag_bits + sum(f.width for f in s.fields)
    return Array(
        name=s.name,
        depth=rows,
        width_bits=entry_bits * s.ways,
        declared_width=entry_bits,
        entries=rows * s.ways,
        origin="sampler",
    )


def budget(
    g: StructureDescription,
    sets: int = DEFAULT_SETS,
    ways: int = DEFAULT_WAYS,
    cores: int = DEFAULT_CORES,
) -> Budget:
    if sets < 1 or ways < 1 or cores < 1:
        raise ValueError(f"bad geometry: sets={sets} ways={ways} cores={cores}")

    arrays: list[Array] = []
    registers: list[Register] = []

    for f in g.state:
        entry = _state_entry(f, sets, ways, cores)
        (arrays if isinstance(entry, Array) else registers).append(entry)
    arrays.extend(_table_entry(t, cores) for t in g.tables)
    arrays.extend(_sampler_entry(s, sets, cores) for s in g.samplers)

    return Budget(
        arrays=tuple(arrays),
        registers=tuple(registers),
        sets=sets,
        ways=ways,
        cores=cores,
    )


def report(b: Budget) -> str:
    lines = [
        f"metadata budget  ({b.sets} sets x {b.ways} ways, {b.cores} core"
        f"{'s' if b.cores != 1 else ''})",
    ]
    for a in b.arrays:
        lines.append(
            f"  SRAM  {a.name:<16} {a.depth:>7} x {a.width_bits:<4} bits"
            f"   = {a.total_bits / 8192:8.2f} KB   [{a.origin}, "
            f"{a.declared_width}b/entry]"
        )
    for r in b.registers:
        lines.append(
            f"  FF    {r.name:<16} {r.width_bits:>7} bits x {r.instances:<4}"
            f"   = {r.total_bits:8d} bits [{r.origin}]"
        )
    lines.append(
        f"  total {b.total_bits / 8192:.2f} KB "
        f"({b.sram_bits / 8192:.2f} KB SRAM + {b.register_bits} FF bits), "
        f"{b.array_bits_per_line():.2f} bits/line array"
        + (f" ({b.bits_per_line():.4f} incl. aux)"
           if b.register_bits else "")
    )
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"usage: {Path(__file__).name} <description.yaml> [...]", file=sys.stderr)
        sys.exit(2)
    for path in sys.argv[1:]:
        g = load(path)
        print(f"\n== {g.name}  ({path})")
        print(report(budget(g)))
