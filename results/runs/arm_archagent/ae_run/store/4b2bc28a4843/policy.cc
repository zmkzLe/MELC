#include "@MODULE@.h"

#include <algorithm>
#include <cmath>
#include "champsim.h"
#include "msl/bits.h"

namespace {
uint32_t CRC_HASH(uint64_t x) {
  uint64_t crc = x;
  for (int i = 0; i < 3; i++)
    crc = (crc & 1) ? ((crc >> 1) ^ 3988292384ULL) : (crc >> 1);
  return static_cast<uint32_t>(crc);
}
}

@MODULE@::@MODULE@(CACHE* cache)
    : replacement(cache), NUM_SET(cache->NUM_SET), NUM_WAY(cache->NUM_WAY),
      log2_llc_set(champsim::lg2(NUM_SET)), log2_sampled_sets(6),
      inf_rd(NUM_WAY * HISTORY - 1), inf_etr(NUM_WAY * HISTORY / GRANULARITY - 1), max_rd(inf_rd - 22),
      sampled_cache_tag_bits(10), pc_signature_bits(15),
      flexmin_penalty(2.0 - std::log2(NUM_CPUS) / 4.0),
      etr(NUM_SET * NUM_WAY, 0), etr_clock(NUM_SET, GRANULARITY), current_timestamp(NUM_SET, 0),
      rdp(1 << pc_signature_bits, -1),
      sampled_cache(1 << (log2_sampled_sets + LOG2_SAMPLED_CACHE_SETS), std::vector<sampled_cache_line>(SAMPLED_CACHE_WAYS)) {}

bool @MODULE@::is_sampled_set(long set) const {
  int mask = (1 << (log2_llc_set - log2_sampled_sets)) - 1;
  return (set & mask) == ((set >> (log2_llc_set - log2_sampled_sets)) & mask);
}

uint32_t @MODULE@::get_pc_signature(uint64_t pc, bool hit, bool prefetch, uint32_t core) const {
  auto shamt = static_cast<unsigned>(64 - pc_signature_bits);
  if (NUM_CPUS == 1) {
    pc <<= 1;
    if (hit) pc |= 1;
    pc <<= 1;
    if (prefetch) pc |= 1;
  } else {
    pc <<= 1;
    if (prefetch) pc |= 1;
    pc <<= 2;
    pc |= core;
  }
  pc = CRC_HASH(pc);
  return static_cast<uint32_t>((pc << shamt) >> shamt);
}

void @MODULE@::detrain(uint32_t index, int way) {
  auto& line = sampled_cache[index][way];
  if (line.valid) {
    int& val = rdp[line.signature];
    val = (val == -1) ? inf_rd : std::min(val + 1, inf_rd);
    line.valid = false;
  }
}

int @MODULE@::temporal_difference(int init, int sample) const {
  if (sample > init) {
    int diff = std::min(1, static_cast<int>((sample - init) * TEMP_DIFFERENCE));
    return std::min(init + diff, inf_rd);
  } else if (sample < init) {
    int diff = std::min(1, static_cast<int>((init - sample) * TEMP_DIFFERENCE));
    return std::max(init - diff, 0);
  }
  return init;
}

int @MODULE@::time_elapsed(int global, int local) {
  return global >= local ? global - local : global + (1 << TIMESTAMP_BITS) - local;
}

long @MODULE@::find_victim(uint32_t cpu, uint64_t, long set, const champsim::cache_block* current_set, champsim::address ip,
                             champsim::address, access_type type) {
  for (long way = 0; way < NUM_WAY; way++) {
    if (!current_set[way].valid) return way;
  }

  int max_etr = 0;
  long victim = 0;
  for (long way = 0; way < NUM_WAY; way++) {
    int val = etr[set * NUM_WAY + way];
    if (std::abs(val) > max_etr || (std::abs(val) == max_etr && val < 0)) {
      max_etr = std::abs(val);
      victim = way;
    }
  }

  uint32_t sig = get_pc_signature(ip.to<uint64_t>(), false, type == access_type::PREFETCH, cpu);
  int pred = rdp[sig];
  if (type != access_type::WRITE && pred != -1 && (pred > max_rd || pred / GRANULARITY > max_etr)) {
    return NUM_WAY;
  }
  return victim;
}

void @MODULE@::update_replacement_state(uint32_t cpu, long set, long way, champsim::address full_addr, champsim::address ip, champsim::address,
                                          access_type type, uint8_t hit) {
  if (hit) access(cpu, set, way, full_addr.to<uint64_t>(), ip.to<uint64_t>(), type, true);
}

void @MODULE@::replacement_cache_fill(uint32_t cpu, long set, long way, champsim::address full_addr, champsim::address ip, champsim::address,
                                        access_type type) {
  access(cpu, set, way, full_addr.to<uint64_t>(), ip.to<uint64_t>(), type, false);
}

void @MODULE@::access(uint32_t cpu, long set, long way, uint64_t full_addr, uint64_t pc, access_type type, bool hit) {
  if (type == access_type::WRITE) {
    if (!hit && way < NUM_WAY) etr[set * NUM_WAY + way] = -inf_etr;
    return;
  }

  uint32_t sig = get_pc_signature(pc, hit, type == access_type::PREFETCH, cpu);

  if (is_sampled_set(set)) {
    uint64_t block = full_addr >> LOG2_BLOCK_SIZE;
    uint32_t i = (block >> log2_llc_set) & ((1 << LOG2_SAMPLED_CACHE_SETS) - 1);
    uint32_t idx = (i << log2_sampled_sets) | (set & ((1 << log2_sampled_sets) - 1));
    uint32_t tag = (block >> (log2_llc_set + LOG2_SAMPLED_CACHE_SETS)) & ((1 << sampled_cache_tag_bits) - 1);

    auto& sampled_set = sampled_cache[idx];
    int hit_way = -1;
    for (int w = 0; w < SAMPLED_CACHE_WAYS; w++) {
      if (sampled_set[w].valid && sampled_set[w].tag == tag) {
        hit_way = w;
        break;
      }
    }

    if (hit_way != -1) {
      auto& line = sampled_set[hit_way];
      int sample = time_elapsed(current_timestamp[set], line.timestamp);
      if (sample <= inf_rd) {
        if (type == access_type::PREFETCH) sample = std::min(static_cast<int>(sample * flexmin_penalty), inf_rd);
        int& val = rdp[line.signature];
        val = (val == -1) ? sample : temporal_difference(val, sample);
        line.valid = false;
      }
    }

    int lru_way = -1, lru_rd = -1;
    for (int w = 0; w < SAMPLED_CACHE_WAYS; w++) {
      if (!sampled_set[w].valid) {
        lru_way = w;
        lru_rd = inf_rd + 1;
        continue;
      }
      int sample = time_elapsed(current_timestamp[set], sampled_set[w].timestamp);
      if (sample > inf_rd) {
        lru_way = w;
        lru_rd = inf_rd + 1;
        detrain(idx, w);
      } else if (sample > lru_rd) {
        lru_way = w;
        lru_rd = sample;
      }
    }
    detrain(idx, lru_way);

    for (auto& line : sampled_set) {
      if (!line.valid) {
        line.valid = true;
        line.signature = sig;
        line.tag = tag;
        line.timestamp = current_timestamp[set];
        break;
      }
    }
    current_timestamp[set] = (current_timestamp[set] + 1) & ((1 << TIMESTAMP_BITS) - 1);
  }

  if (etr_clock[set] == GRANULARITY) {
    for (long w = 0; w < NUM_WAY; w++) {
      if (w != way && std::abs(etr[set * NUM_WAY + w]) < inf_etr) etr[set * NUM_WAY + w]--;
    }
    etr_clock[set] = 0;
  }
  etr_clock[set]++;

  if (way < NUM_WAY) {
    int pred = rdp[sig];
    if (pred == -1) etr[set * NUM_WAY + way] = (NUM_CPUS == 1) ? 0 : inf_etr;
    else if (pred > max_rd) etr[set * NUM_WAY + way] = inf_etr;
    else etr[set * NUM_WAY + way] = pred / GRANULARITY;
  }
}