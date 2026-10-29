#ifndef REPLACEMENT_@MODULE@_H
#define REPLACEMENT_@MODULE@_H

#include <cstdint>
#include <vector>

#include "cache.h"
#include "modules.h"

struct @MODULE@ : public champsim::modules::replacement {
  static constexpr int HISTORY = 8;
  static constexpr int GRANULARITY = 8;
  static constexpr int SAMPLED_CACHE_WAYS = 6;
  static constexpr int LOG2_SAMPLED_CACHE_SETS = 4;
  static constexpr int TIMESTAMP_BITS = 8;
  static constexpr double TEMP_DIFFERENCE = 1.0 / 16.0;

  struct sampled_cache_line {
    bool valid = false;
    uint32_t tag = 0;
    uint32_t signature = 0;
    int timestamp = 0;
  };

  long NUM_SET, NUM_WAY;

  int log2_llc_set, log2_sampled_sets;
  int inf_rd, inf_etr, max_rd;
  int sampled_cache_tag_bits, pc_signature_bits;
  double flexmin_penalty;

  std::vector<int> etr;
  std::vector<int> etr_clock;
  std::vector<int> current_timestamp;
  std::vector<int> rdp;
  std::vector<std::vector<sampled_cache_line>> sampled_cache;

  explicit @MODULE@(CACHE* cache);

  long find_victim(uint32_t triggering_cpu, uint64_t instr_id, long set, const champsim::cache_block* current_set, champsim::address ip,
                   champsim::address full_addr, access_type type);
  void update_replacement_state(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip, champsim::address victim_addr,
                                access_type type, uint8_t hit);
  void replacement_cache_fill(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip, champsim::address victim_addr,
                              access_type type);

private:
  [[nodiscard]] bool is_sampled_set(long set) const;
  [[nodiscard]] uint32_t get_pc_signature(uint64_t pc, bool hit, bool prefetch, uint32_t core) const;
  void detrain(uint32_t index, int way);
  [[nodiscard]] int temporal_difference(int init, int sample) const;
  [[nodiscard]] static int time_elapsed(int global, int local);
  void access(uint32_t cpu, long set, long way, uint64_t full_addr, uint64_t pc, access_type type, bool hit);
};

#endif