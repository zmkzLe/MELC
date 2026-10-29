#include "core_stats.h"

cpu_stats operator-(cpu_stats lhs, cpu_stats rhs)
{
  lhs.begin_instrs -= rhs.begin_instrs;
  lhs.begin_cycles -= rhs.begin_cycles;
  lhs.end_instrs -= rhs.end_instrs;
  lhs.end_cycles -= rhs.end_cycles;
  lhs.total_rob_occupancy_at_branch_mispredict -= rhs.total_rob_occupancy_at_branch_mispredict;

  lhs.total_branch_types -= rhs.total_branch_types;
  lhs.branch_type_misses -= rhs.branch_type_misses;

  lhs.cycles_retiring -= rhs.cycles_retiring;
  lhs.cycles_frontend -= rhs.cycles_frontend;
  lhs.cycles_speculation -= rhs.cycles_speculation;
  lhs.cycles_backend_core -= rhs.cycles_backend_core;
  lhs.cycles_backend_lsq -= rhs.cycles_backend_lsq;
  lhs.cycles_backend_memory_pending -= rhs.cycles_backend_memory_pending;
  for (std::size_t i = 0; i < cpu_stats::MAX_HOPS; ++i) {
    lhs.memory_cycles_by_hops[i] -= rhs.memory_cycles_by_hops[i];
  }

  return lhs;
}
