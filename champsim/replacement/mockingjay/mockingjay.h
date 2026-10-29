#ifndef REPLACEMENT_MOCKINGJAY_H
#define REPLACEMENT_MOCKINGJAY_H

// Mockingjay (Shah, Jain, Lin, HPCA 2022), ported from the authors' CRC2 code
// (github.com/ishanashah/Mockingjay, Apache-2.0). One change: a prefetch-inflated
// reuse-distance sample saturates at INF_RD, as the paper says (Sec. III-D).

#include <cstdint>
#include <unordered_map>
#include <vector>

#include "cache.h"
#include "modules.h"
#include "msl/bits.h"

struct mockingjay : public champsim::modules::replacement {
  static constexpr int HISTORY = 8;
  static constexpr int GRANULARITY = 8;
  static constexpr int SAMPLED_CACHE_WAYS = 5;
  static constexpr int LOG2_SAMPLED_CACHE_SETS = 4;
  static constexpr int TIMESTAMP_BITS = 8;
  static constexpr double TEMP_DIFFERENCE = 1.0 / 16.0;

  struct sampled_cache_line {
    bool valid = false;
    uint64_t tag = 0;
    uint64_t signature = 0;
    int timestamp = 0;
  };

  long NUM_SET, NUM_WAY;

  // The CRC2 code's compile-time constants, derived from this cache's geometry.
  int log2_llc_set, log2_llc_size, log2_sampled_sets;
  int inf_rd, inf_etr, max_rd;
  int sampled_cache_tag_bits, pc_signature_bits;
  double flexmin_penalty;

  std::vector<int> etr;
  std::vector<int> etr_clock;
  std::vector<int> current_timestamp;
  std::unordered_map<uint32_t, int> rdp;
  std::unordered_map<uint32_t, std::vector<sampled_cache_line>> sampled_cache;

  uint64_t bypassed_fills = 0;

  explicit mockingjay(CACHE* cache);

  long find_victim(uint32_t triggering_cpu, uint64_t instr_id, long set, const champsim::cache_block* current_set, champsim::address ip,
                   champsim::address full_addr, access_type type);
  void update_replacement_state(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip, champsim::address victim_addr,
                                access_type type, uint8_t hit);
  void replacement_cache_fill(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip, champsim::address victim_addr,
                              access_type type);
  void replacement_final_stats();

private:
  int& get_etr(long set, long way);
  [[nodiscard]] bool is_sampled_set(long set) const;
  [[nodiscard]] uint64_t get_pc_signature(uint64_t pc, bool hit, bool prefetch, uint32_t core) const;
  [[nodiscard]] uint32_t get_sampled_cache_index(uint64_t full_addr) const;
  [[nodiscard]] uint64_t get_sampled_cache_tag(uint64_t x) const;
  int search_sampled_cache(uint64_t tag, uint32_t index);
  void detrain(uint32_t index, int way);
  [[nodiscard]] int temporal_difference(int init, int sample) const;
  [[nodiscard]] static int increment_timestamp(int input);
  [[nodiscard]] static int time_elapsed(int global, int local);

  // The CRC2 llc_update_replacement_state body, called once per access.
  void access(uint32_t cpu, long set, long way, uint64_t full_addr, uint64_t pc, access_type type, bool hit);
};

#endif
