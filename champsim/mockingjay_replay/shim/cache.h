#ifndef CRC2_SHIM_CACHE_H
#define CRC2_SHIM_CACHE_H

// Minimal CRC2 environment so mockingjay.llc_repl compiles unmodified, with the
// geometry of champsim_config.json's LLC.

#include <cstdint>

#define LLC_SET 4096
#define LLC_WAY 12
#define LOG2_BLOCK_SIZE 6
#define NUM_CPUS 1
#define PREFETCH 2
#define WRITEBACK 3

// The CRC2 file uses log2() in constexpr initialisers of integer constants.
constexpr int log2(int x)
{
  int r = 0;
  while (x >>= 1)
    ++r;
  return r;
}

struct BLOCK {
  bool valid = false;
};

class CACHE
{
public:
  void llc_initialize_replacement();
  uint32_t llc_find_victim(uint32_t cpu, uint64_t instr_id, uint32_t set, const BLOCK* current_set, uint64_t pc, uint64_t full_addr, uint32_t type);
  void llc_update_replacement_state(uint32_t cpu, uint32_t set, uint32_t way, uint64_t full_addr, uint64_t pc, uint64_t victim_addr, uint32_t type,
                                    uint8_t hit);
  void llc_replacement_final_stats();
};

#endif
