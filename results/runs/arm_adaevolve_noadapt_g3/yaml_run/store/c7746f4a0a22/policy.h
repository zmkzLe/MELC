#ifndef REPLACEMENT_@MODULE@_H
#define REPLACEMENT_@MODULE@_H

#include <cstdint>
#include <map>
#include <vector>

#include "cache.h"
#include "modules.h"

class @MODULE@ : public champsim::modules::replacement
{
private:
  // Constants from the YAML description
  const long LOG2_NUM_SET;
  const uint64_t RDP_MAX = 95;
  const int ETR_WIDTH = 5;
  const int RDP_THRESHOLD = 73;
  const int SAMPLER_SETS_PER_GROUP = 16; // 2^4
  const int SAMPLER_WAYS = 5;
  const int SAMPLER_GROUPS = 32;
  const int RDP_ENTRIES = 2048;
  const int PC_SIG_BITS = 11;
  const int SAMPLER_TAG_BITS = 16;

  // State
  std::vector<int8_t> etr;
  std::vector<uint8_t> etr_clock;
  std::vector<uint8_t> set_clock;
  uint16_t bypass_sel;

  // RDP Table
  struct rdp_entry_t {
    uint8_t value = 0;
    bool valid = false;
  };
  std::vector<rdp_entry_t> rdp;

  // Sampler
  struct sampler_entry_t {
    uint16_t signature = 0;
    uint8_t timestamp = 0;
    uint16_t tag = 0;
    bool valid = false;
  };
  std::vector<sampler_entry_t> sampled_cache;
  std::map<long, int> sampled_set_to_group_id;

  // Helper methods
  uint64_t crc3(uint64_t val) const;
  uint64_t get_pc_sig(champsim::address ip, bool hit, access_type type) const;
  bool is_sampled_set(long set) const;
  long get_sampler_set_index(long set, uint64_t line_addr) const;
  uint16_t get_sampler_tag(uint64_t line_addr) const;
  int get_set_sample_category(long set) const;

  rdp_entry_t rdp_read(uint64_t index) const;
  void rdp_train_increment(uint64_t index);
  void rdp_train_toward(uint64_t index, uint64_t sample, uint64_t scale, uint64_t min_diff);

  void procedure_hit_access(uint32_t cpu, long set, long way, champsim::address ip, champsim::address full_addr, access_type type);
  void procedure_fill_access(uint32_t cpu, long set, long way, champsim::address ip, champsim::address full_addr, access_type type);

public:
  long NUM_SET;
  long NUM_WAY;

  explicit @MODULE@(CACHE* cache);

  long find_victim(uint32_t triggering_cpu, uint64_t instr_id, long set, const champsim::cache_block* current_set, champsim::address ip,
                   champsim::address full_addr, access_type type);
  void update_replacement_state(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip, champsim::address victim_addr,
                                access_type type, uint8_t hit);
  void replacement_cache_fill(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip, champsim::address victim_addr,
                              access_type type);
};

#endif
