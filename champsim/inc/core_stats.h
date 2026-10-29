#ifndef CORE_STATS_H
#define CORE_STATS_H

#include <array>
#include <cstdint>
#include <string>

#include "event_counter.h"
#include "instruction.h"

struct cpu_stats {
  std::string name;
  long long begin_instrs = 0;
  long long begin_cycles = 0;
  long long end_instrs = 0;
  long long end_cycles = 0;
  uint64_t total_rob_occupancy_at_branch_mispredict = 0;

  champsim::stats::event_counter<branch_type> total_branch_types = {};
  champsim::stats::event_counter<branch_type> branch_type_misses = {};

  // Every core cycle goes to exactly one of these (O3_CPU::account_cycle).
  uint64_t cycles_retiring = 0;
  uint64_t cycles_frontend = 0;
  uint64_t cycles_speculation = 0;
  uint64_t cycles_backend_core = 0;
  uint64_t cycles_backend_lsq = 0;            // head has a memory op not yet issued to L1D
  uint64_t cycles_backend_memory_pending = 0; // head waits on an issued load; credited by level at retire

  // Pending cycles credited to the level that served the head's last load.
  // Index = hops below the core: 1 = L1D, 2 = L2C, 3 = LLC, 4 = DRAM. 0 = never served, last = deeper.
  static constexpr std::size_t MAX_HOPS = 6;
  std::array<uint64_t, MAX_HOPS> memory_cycles_by_hops = {};

  [[nodiscard]] auto instrs() const { return end_instrs - begin_instrs; }
  [[nodiscard]] auto cycles() const { return end_cycles - begin_cycles; }
  [[nodiscard]] auto cycles_accounted() const
  {
    return cycles_retiring + cycles_frontend + cycles_speculation + cycles_backend_core + cycles_backend_lsq + cycles_backend_memory_pending;
  }
  [[nodiscard]] auto memory_cycles_credited() const
  {
    uint64_t sum = 0;
    for (auto v : memory_cycles_by_hops) {
      sum += v;
    }
    return sum;
  }
};

cpu_stats operator-(cpu_stats lhs, cpu_stats rhs);

#endif
