from __future__ import annotations

from pathlib import Path

CHAMPSIM = Path.home() / "champsim"
POLICIES = CHAMPSIM / "structure_description" / "policies"

STRUCTURE_DESCRIPTION, HEADER, SOURCE = "STRUCTURE_DESCRIPTION", "HEADER", "SOURCE"
MARKER = "===== {} ====="

MODULE = "evolved_policy"

INSTRUCTIONS = """\
You design last-level-cache replacement policies for ChampSim and write the C++
that implements them.

Return the whole candidate as ONE file inside a SINGLE fenced code block. Do
not open a separate block per section: only the first block in your reply is
read, so a structure description in its own block arrives with both C++ files
missing and the candidate is discarded unscored.

```
===== STRUCTURE_DESCRIPTION =====
<structure description YAML>
===== HEADER =====
<the module header>
===== SOURCE =====
<the module source>
```

All three `=====` marker lines must appear, spelled exactly as above.

The machine is the DPC4 single-core LLC: 4096 sets, 12 ways, 35-cycle latency,
no prefetcher anywhere. The score is the geometric mean of IPC relative to Mockingjay
over the training benchmarks; higher is better.

Each candidate also reports what its metadata costs, priced by CACTI at 22 nm
from the state the structure description declares: metadata_kb (SRAM),
metadata_area_mm2, and metadata_energy_uj (summed over the training traces).
These are reported, not scored. They are computed from the structure
description, so declare every table and field the C++ keeps.

ChampSim's interface, which the seed's C++ shows in full:
- The class is named @MODULE@, literally, wherever a class name appears: `class
  @MODULE@ : public champsim::modules::replacement`, a constructor `explicit
  @MODULE@(CACHE* cache);` that initialises `replacement(cache)`, the header
  guard REPLACEMENT_@MODULE@_H, and the source's `#include "@MODULE@.h"`.
- The header includes "cache.h" and "modules.h". Declare `long NUM_SET;` and
  `long NUM_WAY;` as members and set them from cache->NUM_SET and cache->NUM_WAY
  in the constructor's initialiser list; `cache` exists only in the constructor.
  NUM_CPUS and LOG2_BLOCK_SIZE are ChampSim constants; include "champsim.h" in
  the source for NUM_CPUS. champsim::lg2 is in "msl/bits.h". There is no
  champsim_constants.h.
- The three hooks have exactly the seed's signatures: find_victim,
  update_replacement_state, replacement_cache_fill. Declare nothing else public.
  No `override` and no `virtual`: ChampSim finds the hooks by name, and the base
  class declares none of them.
- ip and full_addr are champsim::address; read them with .to<uint64_t>().
  full_addr keeps its bit positions with the 6 offset bits zeroed, so the line
  address is full_addr >> LOG2_BLOCK_SIZE.
- Access types are access_type::LOAD, RFO, PREFETCH, WRITE and TRANSLATION.
- find_victim returns a way in 0..NUM_WAY-1, or NUM_WAY to bypass.
- `way` may equal NUM_WAY in update_replacement_state (a miss) and in
  replacement_cache_fill (a bypassed fill): never index per-line storage with it.
- Print nothing. No randomness, no static or global mutable state.

What will otherwise cost you the candidate:
- Write @MODULE@ wherever the class name goes, in both the header and the
  source. It is substituted before the build; a literal class name will not link.
- Both a header and a source are required. A single header does not build.
- The structure description must pass the closed schema: every field is a
  bounded enum or number and unknown keys are refused. Use only vocabulary that
  appears in the seed's structure description.
- The structure description and the C++ must describe the same policy.
- Never bypass a fill of type WRITE (a writeback): ChampSim aborts that run,
  and a candidate is refused unless every training trace finishes.
"""


def templated_seed(name: str = "mockingjay") -> tuple[str, str, str]:
    structure_description = (POLICIES / f"{name}.yaml").read_text()
    directory = CHAMPSIM / "replacement" / name
    header = (directory / f"{name}.h").read_text()
    source = (directory / f"{name}.cc").read_text()
    for old in (name.upper(), name):
        replacement = "@MODULE@_H_GUARD" if old == name.upper() else "@MODULE@"
        header = header.replace(old, replacement)
        source = source.replace(old, replacement)
    return structure_description, header, source


def module_sources(header: str, source: str, module: str = MODULE) -> dict[str, str]:
    return {f"{module}.h": header.replace("@MODULE@", module),
            f"{module}.cc": source.replace("@MODULE@", module)}


def seed_sources(name: str = "mockingjay", module: str = MODULE) -> dict[str, str]:
    _, header, source = templated_seed(name)
    return module_sources(header, source, module)


def packed_seed(name: str = "mockingjay") -> str:
    structure_description, header, source = templated_seed(name)
    return (f"{MARKER.format(STRUCTURE_DESCRIPTION)}\n{structure_description}\n"
            f"{MARKER.format(HEADER)}\n{header}\n"
            f"{MARKER.format(SOURCE)}\n{source}\n")
