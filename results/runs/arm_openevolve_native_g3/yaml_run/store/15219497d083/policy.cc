#include "@MODULE@.h"

#include <algorithm>
#include <cmath>

#include "champsim.h"
#include "msl/bits.h"

@MODULE@::@MODULE@(CACHE* cache)
: champsim::modules::replacement(cache), LOG2_NUM_SET(champsim::lg2(cache->NUM_SET)), etr(cache->NUM_SET * cache->NUM_WAY, 0),
  etr_clock(cache->NUM_SET, 8), set_clock(cache->NUM_SET, 0), rdp(RDP_ENTRIES), bypass_conf(2048, 3),
  sampled_cache(SAMPLER_GROUPS * SAMPLER_SETS_PER_GROUP * SAMPLER_WAYS), NUM_SET(cache->NUM_SET), NUM_WAY(cache->NUM_WAY)
{
  int group_id = 0;
  for (long i = 0; i < NUM_SET; ++i) {
    if (is_sampled_set(i)) {
      if (group_id < SAMPLER_GROUPS) {
        sampled_set_to_group_id[i] = group_id++;
      }
    }
  }
}

long @MODULE@::find_victim(uint32_t triggering_cpu, uint64_t instr_id, long set, const champsim::cache_block* current_set, champsim::address ip,
                           champsim::address full_addr, access_type type)
{
  long victim_way = 0;
  long max_abs_etr = 0;

  for (long w = 0; w < NUM_WAY; ++w) {
    int8_t current_etr = etr.at(set * NUM_WAY + w);
    long current_abs_etr = std::abs(current_etr);

    if (current_abs_etr > max_abs_etr) {
      max_abs_etr = current_abs_etr;
      victim_way = w;
    } else if (current_abs_etr == max_abs_etr) {
      if (current_etr < 0) {
        victim_way = w;
      }
    }
  }

  if (type == access_type::WRITE) {
    return victim_way;
  }

  uint64_t sig = get_pc_sig(ip, false, type);

  if (confident_bypass(sig, 73, 2)) {
    return NUM_WAY;
  }

  auto rdp_entry = rdp_read(sig);
  if (rdp_entry.valid) {
    int8_t victim_etr = etr.at(set * NUM_WAY + victim_way);
    if ((rdp_entry.value / 8) > std::abs(victim_etr) && bypass_conf.at(sig) >= 1) {
      return NUM_WAY;
    }
  }

  return victim_way;
}

void @MODULE@::update_replacement_state(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip,
                                        champsim::address victim_addr, access_type type, uint8_t hit)
{
  if (hit) {
    if (type == access_type::WRITE) {
      return; // stop
    }
    procedure_access(triggering_cpu, set, way, ip, full_addr, type, true);
  }
}

void @MODULE@::replacement_cache_fill(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip,
                                      champsim::address victim_addr, access_type type)
{
  if (type == access_type::WRITE) {
    if (way < NUM_WAY) {
      etr.at(set * NUM_WAY + way) = -ETR_MAX_ABS;
    }
    return; // stop
  }
  procedure_access(triggering_cpu, set, way, ip, full_addr, type, false);
}

uint64_t @MODULE@::crc3(uint64_t val) const
{
  for (int i = 0; i < 3; ++i) {
    if (val & 1) {
      val = (val >> 1) ^ 0xEDB88320;
    } else {
      val = val >> 1;
    }
  }
  return val;
}

uint64_t @MODULE@::get_pc_sig(champsim::address ip, bool hit, access_type type) const
{
  uint64_t val = ip.to<uint64_t>();
  val = (val << 1) | (hit ? 1 : 0);
  val = (val << 1) | (type == access_type::PREFETCH ? 1 : 0);
  val = crc3(val);
  return val & ((1 << PC_SIG_BITS) - 1);
}

bool @MODULE@::is_sampled_set(long set) const
{
  const int m = LOG2_NUM_SET - champsim::lg2(SAMPLER_GROUPS);
  const uint64_t mask = (1 << m) - 1;
  return (static_cast<uint64_t>(set) & mask) == ((static_cast<uint64_t>(set) >> champsim::lg2(SAMPLER_GROUPS)) & mask);
}

long @MODULE@::get_sampler_set_index(long set, uint64_t line_addr) const
{
  auto it = sampled_set_to_group_id.find(set);
  int group_id = it->second;
  uint64_t sampler_set_offset = (line_addr >> LOG2_NUM_SET) & (SAMPLER_SETS_PER_GROUP - 1);
  return group_id * SAMPLER_SETS_PER_GROUP + sampler_set_offset;
}

uint16_t @MODULE@::get_sampler_tag(uint64_t line_addr) const
{
  return (line_addr >> (LOG2_NUM_SET + 4)) & ((1 << SAMPLER_TAG_BITS) - 1);
}

@MODULE@::rdp_entry_t @MODULE@::rdp_read(uint64_t index) const
{
  return rdp.at(index);
}

void @MODULE@::rdp_train_ema(uint64_t index, uint64_t sample, uint64_t scale, uint64_t shift)
{
  uint64_t target = std::min(sample * scale, RDP_MAX);
  if (!rdp.at(index).valid) {
    rdp.at(index).value = target;
    rdp.at(index).valid = true;
  } else {
    long long diff = static_cast<long long>(target) - static_cast<long long>(rdp.at(index).value);
    long long step = diff / (1LL << shift);
    long long new_val = static_cast<long long>(rdp.at(index).value) + step;
    if (new_val < 0) {
      rdp.at(index).value = 0;
    } else if (new_val > static_cast<long long>(RDP_MAX)) {
      rdp.at(index).value = RDP_MAX;
    } else {
      rdp.at(index).value = static_cast<uint8_t>(new_val);
    }
  }
}

void @MODULE@::rdp_train_penalty(uint64_t index, uint64_t penalty)
{
  if (!rdp.at(index).valid) {
    rdp.at(index).value = RDP_MAX;
    rdp.at(index).valid = true;
  } else {
    uint64_t new_val = rdp.at(index).value + penalty;
    if (new_val > RDP_MAX) {
      rdp.at(index).value = RDP_MAX;
    } else {
      rdp.at(index).value = static_cast<uint8_t>(new_val);
    }
  }
}

void @MODULE@::bypass_conf_train_increment(uint64_t index)
{
  if (bypass_conf.at(index) < 3) {
    bypass_conf.at(index)++;
  }
}

void @MODULE@::bypass_conf_train_decrement(uint64_t index)
{
  if (bypass_conf.at(index) > 0) {
    bypass_conf.at(index)--;
  }
}

bool @MODULE@::confident_bypass(uint64_t index, uint64_t rdp_thresh, uint64_t conf_thresh) const
{
  auto rdp_entry = rdp_read(index);
  if (!rdp_entry.valid) {
    return false;
  }
  uint8_t conf_val = bypass_conf.at(index);
  return (rdp_entry.value > rdp_thresh) && (conf_val >= conf_thresh);
}

void @MODULE@::procedure_access(uint32_t cpu, long set, long way, champsim::address ip, champsim::address full_addr, access_type type, bool hit)
{
  // sample: sampled_cache
  if (is_sampled_set(set)) {
    uint64_t line_addr = full_addr.to<uint64_t>() >> LOG2_BLOCK_SIZE;
    long sampler_set_idx = get_sampler_set_index(set, line_addr);
    uint16_t tag = get_sampler_tag(line_addr);

    long sampler_set_start = sampler_set_idx * SAMPLER_WAYS;
    long sampler_set_end = sampler_set_start + SAMPLER_WAYS;

    long match_way = -1;
    for (long w = sampler_set_start; w < sampler_set_end; ++w) {
      if (sampled_cache.at(w).valid && sampled_cache.at(w).tag == tag) {
        match_way = w;
        break;
      }
    }

    if (match_way != -1) {
      auto& entry = sampled_cache.at(match_way);
      uint8_t age = (set_clock.at(set) - entry.timestamp); // 8-bit wrapping
      if (age <= 95) {
        if (age > 73) {
          bypass_conf_train_increment(entry.signature);
        }
        if (age <= 73) {
          bypass_conf_train_decrement(entry.signature);
        }
        if (age <= 73) {
          bypass_conf_train_decrement(entry.signature);
        }
        if (type == access_type::PREFETCH) {
          rdp_train_ema(entry.signature, age, 2, 2);
          entry.valid = false;
        } else {
          rdp_train_ema(entry.signature, age, 1, 2);
          entry.valid = false;
        }
      }
    }

    for (long w = sampler_set_start; w < sampler_set_end; ++w) {
      auto& entry = sampled_cache.at(w);
      if (entry.valid) {
        uint8_t age = (set_clock.at(set) - entry.timestamp);
        if (age > 95) {
          bypass_conf_train_increment(entry.signature);
        }
        if (age > 95) {
          rdp_train_penalty(entry.signature, 8);
          entry.valid = false;
        }
      }
    }

    long final_match_way = -1;
    for (long w = sampler_set_start; w < sampler_set_end; ++w) {
      if (sampled_cache.at(w).valid && sampled_cache.at(w).tag == tag) {
        final_match_way = w;
        break;
      }
    }

    if (final_match_way == -1) {
      long victim_way = -1;
      for (long w = sampler_set_start; w < sampler_set_end; ++w) {
        if (!sampled_cache.at(w).valid) {
          victim_way = w;
          break;
        }
      }

      if (victim_way == -1) {
        uint8_t max_age = 0;
        victim_way = sampler_set_start;
        for (long w = sampler_set_start; w < sampler_set_end; ++w) {
          uint8_t age = (set_clock.at(set) - sampled_cache.at(w).timestamp);
          if (age > max_age) {
            max_age = age;
            victim_way = w;
          }
        }
      }

      auto& victim_entry = sampled_cache.at(victim_way);
      if (victim_entry.valid) {
        bypass_conf_train_increment(victim_entry.signature);
        rdp_train_penalty(victim_entry.signature, 8);
      }

      victim_entry.signature = get_pc_sig(ip, hit, type);
      victim_entry.timestamp = set_clock.at(set);
      victim_entry.tag = tag;
      victim_entry.valid = true;
    }

    set_clock.at(set)++;
  }

  // etr aging
  if (etr_clock.at(set) == 8) {
    for (long w = 0; w < NUM_WAY; ++w) {
      if (w != way) {
        if (std::abs(etr.at(set * NUM_WAY + w)) < ETR_MAX_ABS) {
          etr.at(set * NUM_WAY + w)--;
        }
      }
    }
    etr_clock.at(set) = 0;
  }
  etr_clock.at(set) = (etr_clock.at(set) + 1) & 0xF;

  // set etr for current access
  if (way < NUM_WAY) {
    uint64_t sig = get_pc_sig(ip, hit, type);
    auto rdp_entry = rdp_read(sig);

    if (!rdp_entry.valid) {
      etr.at(set * NUM_WAY + way) = 0;
      return; // stop
    }
    etr.at(set * NUM_WAY + way) = rdp_entry.value / 8;
  }
}
