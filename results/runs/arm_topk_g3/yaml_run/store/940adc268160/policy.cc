#include "@MODULE@.h"

#include "champsim.h"

@MODULE@::@MODULE@(CACHE* cache)
: champsim::modules::replacement(cache), example_state(cache->NUM_SET * cache->NUM_WAY, 0), NUM_SET(cache->NUM_SET), NUM_WAY(cache->NUM_WAY)
{
}

long @MODULE@::find_victim(uint32_t triggering_cpu, uint64_t instr_id, long set, const champsim::cache_block* current_set, champsim::address ip,
                           champsim::address full_addr, access_type type)
{
  long victim_way = 0;
  uint8_t max_val = 0;

  for (long w = 0; w < NUM_WAY; ++w) {
    uint8_t val = example_state.at(set * NUM_WAY + w);
    if (val > max_val) {
      max_val = val;
      victim_way = w;
    }
  }

  return victim_way;
}

void @MODULE@::update_replacement_state(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip,
                                        champsim::address victim_addr, access_type type, uint8_t hit)
{
}

void @MODULE@::replacement_cache_fill(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip,
                                      champsim::address victim_addr, access_type type)
{
  if (way != NUM_WAY) {
    example_state.at(set * NUM_WAY + way) = 0;
  }
}
