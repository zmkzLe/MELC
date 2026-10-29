#include "@MODULE@.h"

#include <algorithm>
#include <cmath>

#include "champsim.h"

@MODULE@::@MODULE@(CACHE* cache)
    : replacement(cache),
      NUM_SET(cache->NUM_SET),
      NUM_WAY(cache->NUM_WAY),
      etr(static_cast<size_t>(cache->NUM_SET * cache->NUM_WAY), 0),
      etr_clock(static_cast<size_t>(cache->NUM_SET), 8),
      set_clock(static_cast<size_t>(cache->NUM_SET), 0),
      rdp(RDP_ENTRIES, -1),
      sampled_cache(static_cast<size_t>(NUM_SAMPLED_SETS * (1 << EXTRA_INDEX_BITS) * SAMPLED_CACHE_WAYS)) {}

uint32_t @MODULE@::get_pc_signature(champsim::address ip, bool hit, bool prefetch) {
  uint64_t val = ip.to<uint64_t>();
  val ^= (hit ? 1ULL : 0ULL);
  val ^= (prefetch ? 2ULL : 0ULL);
  val ^= (val >> 11);
  val ^= (val >> 22);
  val ^= (val >> 33);
  return static_cast<uint32_t>(val & (RDP_ENTRIES - 1));
}

bool @MODULE@::is_sampled_set(long set) const {
  long stride = NUM_SET / NUM_SAMPLED_SETS;
  return (set % stride) == 0;
}

long @MODULE@::get_sampler_set(long set, champsim::address full_addr) const {
  long stride = NUM_SET / NUM_SAMPLED_SETS;
  long sampled_id = set / stride;
  uint64_t line_addr = full_addr.to<uint64_t>() >> LOG2_BLOCK_SIZE;
  uint64_t extra_bits = (line_addr >> 12) & ((1 << EXTRA_INDEX_BITS) - 1);
  return (sampled_id << EXTRA_INDEX_BITS) | extra_bits;
}

long @MODULE@::find_victim(uint32_t triggering_cpu, uint64_t instr_id, long set, const champsim::cache_block* current_set, champsim::address ip,
                           champsim::address full_addr, access_type type) {
  for (long way = 0; way < NUM_WAY; ++way) {
    if (current_set[way].valid == 0) {
      return way;
    }
  }

  long victim_way = 0;
  int max_mag = -1;
  bool victim_neg = false;

  for (long way = 0; way < NUM_WAY; ++way) {
    int val = etr[set * NUM_WAY + way];
    int mag = std::abs(val);
    bool is_neg = (val < 0);

    if (mag > max_mag) {
      max_mag = mag;
      victim_neg = is_neg;
      victim_way = way;
    } else if (mag == max_mag) {
      if (is_neg && !victim_neg) {
        victim_neg = true;
        victim_way = way;
      }
    }
  }

  if (type != access_type::WRITE) {
    uint32_t sig = get_pc_signature(ip, false, type == access_type::PREFETCH);
    int pred_rd = rdp[sig];
    if (pred_rd != -1) {
      if (pred_rd > MAX_RD || (pred_rd / 8) > max_mag) {
        return NUM_WAY;
      }
    }
  }

  return victim_way;
}

void @MODULE@::access_procedure(long set, long way, champsim::address full_addr, champsim::address ip, access_type type, bool hit) {
  uint32_t sig = get_pc_signature(ip, hit, type == access_type::PREFETCH);

  if (is_sampled_set(set)) {
    long sset = get_sampler_set(set, full_addr);
    uint64_t line_addr = full_addr.to<uint64_t>() >> LOG2_BLOCK_SIZE;
    uint32_t tag = static_cast<uint32_t>((line_addr >> 16) & 0x3FF);

    long base = sset * SAMPLED_CACHE_WAYS;
    bool matched = false;

    for (long i = 0; i < SAMPLED_CACHE_WAYS; ++i) {
      SamplerEntry& entry = sampled_cache[base + i];
      if (entry.valid && entry.tag == tag) {
        matched = true;
        uint8_t age = static_cast<uint8_t>(set_clock[set] - entry.timestamp);
        if (age <= INF_RD) {
          int cur_rd = (rdp[entry.signature] == -1) ? 0 : rdp[entry.signature];
          int diff = static_cast<int>(age) - cur_rd;
          int step = diff / 16;
          if (step == 0 && diff != 0) {
            step = (diff > 0) ? 1 : -1;
          }
          if (type == access_type::PREFETCH) {
            step /= 2;
          }
          int next_rd = std::clamp(cur_rd + step, 0, INF_RD);
          rdp[entry.signature] = static_cast<int8_t>(next_rd);
          entry.valid = false;
        }
        break;
      }
    }

    if (!matched) {
      long victim_idx = -1;
      int max_age = -1;

      for (long i = 0; i < SAMPLED_CACHE_WAYS; ++i) {
        SamplerEntry& entry = sampled_cache[base + i];
        if (entry.valid) {
          uint8_t age = static_cast<uint8_t>(set_clock[set] - entry.timestamp);
          if (age > INF_RD) {
            if (rdp[entry.signature] != -1 && rdp[entry.signature] < INF_RD) {
              rdp[entry.signature]++;
            }
            entry.valid = false;
          }
        }
        if (!entry.valid && victim_idx == -1) {
          victim_idx = i;
        }
      }

      if (victim_idx == -1) {
        for (long i = 0; i < SAMPLED_CACHE_WAYS; ++i) {
          SamplerEntry& entry = sampled_cache[base + i];
          uint8_t age = static_cast<uint8_t>(set_clock[set] - entry.timestamp);
          if (static_cast<int>(age) > max_age) {
            max_age = age;
            victim_idx = i;
          }
        }
        if (victim_idx != -1) {
          SamplerEntry& v_entry = sampled_cache[base + victim_idx];
          if (rdp[v_entry.signature] != -1 && rdp[v_entry.signature] < INF_RD) {
            rdp[v_entry.signature]++;
          }
        }
      }

      if (victim_idx != -1) {
        SamplerEntry& entry = sampled_cache[base + victim_idx];
        entry.valid = true;
        entry.tag = tag;
        entry.signature = sig;
        entry.timestamp = set_clock[set];
      }
    }

    set_clock[set] = static_cast<uint8_t>((set_clock[set] + 1) & 0xFF);
  }

  if (etr_clock[set] >= GRANULARITY) {
    for (long w = 0; w < NUM_WAY; ++w) {
      if (w != way) {
        long idx = set * NUM_WAY + w;
        if (std::abs(etr[idx]) < INF_ETR) {
          etr[idx] = static_cast<int8_t>(etr[idx] - 1);
        }
      }
    }
    etr_clock[set] = 0;
  }
  etr_clock[set]++;

  if (way < NUM_WAY) {
    int pred_rd = rdp[sig];
    if (pred_rd == -1) {
      etr[set * NUM_WAY + way] = 0;
    } else if (pred_rd > MAX_RD) {
      etr[set * NUM_WAY + way] = INF_ETR;
    } else {
      etr[set * NUM_WAY + way] = static_cast<int8_t>(pred_rd / 8);
    }
  }
}

void @MODULE@::update_replacement_state(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip,
                                        champsim::address victim_addr, access_type type, uint8_t hit) {
  if (hit) {
    if (type == access_type::WRITE) {
      return;
    }
    access_procedure(set, way, full_addr, ip, type, true);
  }
}

void @MODULE@::replacement_cache_fill(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip,
                                      champsim::address victim_addr, access_type type) {
  if (way >= NUM_WAY) {
    return;
  }

  if (type == access_type::WRITE) {
    etr[set * NUM_WAY + way] = -INF_ETR;
    return;
  }

  access_procedure(set, way, full_addr, ip, type, false);
}