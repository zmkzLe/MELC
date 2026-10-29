#ifndef REPLACEMENT_@MODULE@_H
#define REPLACEMENT_@MODULE@_H

#include <cstdint>
#include <vector>

#include "cache.h"
#include "modules.h"

struct @MODULE@ : public champsim::modules::replacement {
  static constexpr int GRANULARITY = 8;
  static constexpr int SAMPLED_CACHE_WAYS = 5;
  static constexpr int EXTRA_INDEX_BITS = 4;
  static constexpr int NUM_SAMPLED_SETS = 32;
  static constexpr int INF_RD = 95;
  static constexpr int INF_ETR = 11;
  static constexpr int MAX_RD = 73;
  static constexpr int RDP_ENTRIES = 2048;

  struct SamplerEntry {
    bool valid = false;
    uint32_t tag = 0;
    uint32_t signature = 0;
    uint8_t timestamp = 0;
  };

  long NUM_SET;
  long NUM_WAY;

  std::vector<int8_t> etr;
  std::vector<uint8_t> etr_clock;
  std::vector<uint8_t> set_clock;
  std::vector<int8_t> rdp;
  std::vector<SamplerEntry> sampled_cache;

  explicit @MODULE@(CACHE* cache);

  long find_victim(uint32_t triggering_cpu, uint64_t instr_id, long set, const champsim::cache_block* current_set, champsim::address ip,
                   champsim::address full_addr, access_type type);
  void update_replacement_state(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip,
                                champsim::address victim_addr, access_type type, uint8_t hit);
  void replacement_cache_fill(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip,
                              champsim::address victim_addr, access_type type);

  uint32_t get_pc_signature(champsim::address ip, bool hit, bool prefetch);
  bool is_sampled_set(long set) const;
  long get_sampler_set(long set, champsim::address full_addr) const;
  void access_procedure(long set, long way, champsim::address full_addr, champsim::address ip, access_type type, bool hit);
};

#endif