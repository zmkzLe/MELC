#include "@MODULE@.h"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <fmt/core.h>

#include "champsim.h"

namespace
{
uint64_t CRC_HASH(uint64_t _blockAddress)
{
  static const unsigned long long crcPolynomial = 3988292384ULL;
  unsigned long long _returnVal = _blockAddress;
  for (unsigned int i = 0; i < 3; i++)
    _returnVal = ((_returnVal & 1) == 1) ? ((_returnVal >> 1) ^ crcPolynomial) : (_returnVal >> 1);
  return _returnVal;
}
} // namespace

@MODULE@::@MODULE@(CACHE* cache)
    : replacement(cache), NUM_SET(cache->NUM_SET), NUM_WAY(cache->NUM_WAY), log2_llc_set(static_cast<int>(champsim::lg2(NUM_SET))),
      log2_llc_size(log2_llc_set + static_cast<int>(champsim::lg2(NUM_WAY)) + static_cast<int>(LOG2_BLOCK_SIZE)), log2_sampled_sets(6),
      inf_rd(static_cast<int>(NUM_WAY) * HISTORY - 1), inf_etr(static_cast<int>(NUM_WAY) * HISTORY / GRANULARITY - 1), max_rd(inf_rd - 22),
      sampled_cache_tag_bits(10), pc_signature_bits(13),
      flexmin_penalty(2.0 - std::log2(static_cast<double>(NUM_CPUS)) / 4.0), etr(static_cast<std::size_t>(NUM_SET * NUM_WAY), 0),
      etr_clock(static_cast<std::size_t>(NUM_SET), GRANULARITY), current_timestamp(static_cast<std::size_t>(NUM_SET), 0)
{
  for (long set = 0; set < NUM_SET; set++) {
    if (is_sampled_set(set)) {
      long modifier = 1L << log2_llc_set;
      long limit = 1L << LOG2_SAMPLED_CACHE_SETS;
      for (long i = 0; i < limit; i++)
        sampled_cache.emplace(static_cast<uint32_t>(set + modifier * i), std::vector<sampled_cache_line>(SAMPLED_CACHE_WAYS));
    }
  }
}

int& @MODULE@::get_etr(long set, long way) { return etr.at(static_cast<std::size_t>(set * NUM_WAY + way)); }

bool @MODULE@::is_sampled_set(long set) const
{
  int mask_length = log2_llc_set - log2_sampled_sets;
  long mask = (1L << mask_length) - 1;
  return (set & mask) == ((set >> (log2_llc_set - mask_length)) & mask);
}

uint64_t @MODULE@::get_pc_signature(uint64_t pc, bool hit, bool prefetch, uint32_t core) const
{
  auto shamt = static_cast<unsigned>(64 - pc_signature_bits);
  if (NUM_CPUS == 1) {
    pc = pc << 1;
    if (hit)
      pc = pc | 1;
    pc = pc << 1;
    if (prefetch)
      pc = pc | 1;
    pc = CRC_HASH(pc);
    pc = (pc << shamt) >> shamt;
  } else {
    pc = pc << 1;
    if (prefetch)
      pc = pc | 1;
    pc = pc << 2;
    pc = pc | core;
    pc = CRC_HASH(pc);
    pc = (pc << shamt) >> shamt;
  }
  return pc;
}

// ChampSim's module address keeps bit positions (offset zeroed), so the shift still applies.
uint32_t @MODULE@::get_sampled_cache_index(uint64_t full_addr) const
{
  auto shamt = static_cast<unsigned>(64 - (LOG2_SAMPLED_CACHE_SETS + log2_llc_set));
  full_addr = full_addr >> LOG2_BLOCK_SIZE;
  full_addr = (full_addr << shamt) >> shamt;
  return static_cast<uint32_t>(full_addr);
}

uint64_t @MODULE@::get_sampled_cache_tag(uint64_t x) const
{
  auto shamt = static_cast<unsigned>(64 - sampled_cache_tag_bits);
  x >>= static_cast<unsigned>(log2_llc_set) + LOG2_BLOCK_SIZE + LOG2_SAMPLED_CACHE_SETS;
  x = (x << shamt) >> shamt;
  return x;
}

int @MODULE@::search_sampled_cache(uint64_t tag, uint32_t index)
{
  const auto& sampled_set = sampled_cache.at(index);
  for (int way = 0; way < SAMPLED_CACHE_WAYS; way++) {
    if (sampled_set[static_cast<std::size_t>(way)].valid && sampled_set[static_cast<std::size_t>(way)].tag == tag)
      return way;
  }
  return -1;
}

void @MODULE@::detrain(uint32_t index, int way)
{
  auto& line = sampled_cache.at(index).at(static_cast<std::size_t>(way));
  if (!line.valid)
    return;

  auto signature = static_cast<uint32_t>(line.signature);
  if (auto it = rdp.find(signature); it != rdp.end())
    it->second = std::min(it->second + 1, inf_rd);
  else
    rdp[signature] = inf_rd;
  line.valid = false;
}

int @MODULE@::temporal_difference(int init, int sample) const
{
  if (sample > init) {
    int diff = sample - init;
    diff = static_cast<int>(diff * TEMP_DIFFERENCE);
    diff = std::min(1, diff);
    return std::min(init + diff, inf_rd);
  } else if (sample < init) {
    int diff = init - sample;
    diff = static_cast<int>(diff * TEMP_DIFFERENCE);
    diff = std::min(1, diff);
    return std::max(init - diff, 0);
  } else {
    return init;
  }
}

int @MODULE@::increment_timestamp(int input)
{
  input++;
  input = input % (1 << TIMESTAMP_BITS);
  return input;
}

int @MODULE@::time_elapsed(int global, int local)
{
  if (global >= local)
    return global - local;
  global = global + (1 << TIMESTAMP_BITS);
  return global - local;
}

long @MODULE@::find_victim(uint32_t triggering_cpu, uint64_t, long set, const champsim::cache_block* current_set, champsim::address ip,
                             champsim::address, access_type type)
{
  for (long way = 0; way < NUM_WAY; way++) {
    if (!current_set[way].valid)
      return way;
  }

  int max_etr = 0;
  long victim_way = 0;
  for (long way = 0; way < NUM_WAY; way++) {
    int value = get_etr(set, way);
    if (std::abs(value) > max_etr || (std::abs(value) == max_etr && value < 0)) {
      max_etr = std::abs(value);
      victim_way = way;
    }
  }

  // Bypass (return NUM_WAY) when the incoming line is predicted to be reused after every line in the set.
  auto pc_signature = static_cast<uint32_t>(get_pc_signature(ip.to<uint64_t>(), false, type == access_type::PREFETCH, triggering_cpu));
  if (auto it = rdp.find(pc_signature);
      type != access_type::WRITE && it != rdp.end() && (it->second > max_rd || it->second / GRANULARITY > max_etr)) {
    ++bypassed_fills;
    return NUM_WAY;
  }

  return victim_way;
}

// Each access is handled once: hits here, misses at their fill or bypass.
// ChampSim also calls this on the miss itself (way == NUM_WAY); that call is ignored.
void @MODULE@::update_replacement_state(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip, champsim::address,
                                          access_type type, uint8_t hit)
{
  if (hit)
    access(triggering_cpu, set, way, full_addr.to<uint64_t>(), ip.to<uint64_t>(), type, true);
}

void @MODULE@::replacement_cache_fill(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip, champsim::address,
                                        access_type type)
{
  access(triggering_cpu, set, way, full_addr.to<uint64_t>(), ip.to<uint64_t>(), type, false);
}

void @MODULE@::access(uint32_t cpu, long set, long way, uint64_t full_addr, uint64_t pc, access_type type, bool hit)
{
  if (type == access_type::WRITE) {
    if (!hit)
      get_etr(set, way) = -inf_etr;
    return;
  }

  pc = get_pc_signature(pc, hit, type == access_type::PREFETCH, cpu);
  auto set_idx = static_cast<std::size_t>(set);

  if (is_sampled_set(set)) {
    uint32_t sampled_cache_index = get_sampled_cache_index(full_addr);
    uint64_t sampled_cache_tag = get_sampled_cache_tag(full_addr);
    int sampled_cache_way = search_sampled_cache(sampled_cache_tag, sampled_cache_index);
    auto& sampled_set = sampled_cache.at(sampled_cache_index);

    if (sampled_cache_way > -1) {
      auto& hit_line = sampled_set[static_cast<std::size_t>(sampled_cache_way)];
      auto last_signature = static_cast<uint32_t>(hit_line.signature);
      int sample = time_elapsed(current_timestamp[set_idx], hit_line.timestamp);

      if (sample <= inf_rd) {
        if (type == access_type::PREFETCH) {
          // Paper Sec. III-D: the inflated sample saturates at INF_RD.
          sample = std::min(static_cast<int>(sample * flexmin_penalty), inf_rd);
        }
        if (auto it = rdp.find(last_signature); it != rdp.end())
          it->second = temporal_difference(it->second, sample);
        else
          rdp[last_signature] = sample;

        hit_line.valid = false;
      }
    }

    int lru_way = -1;
    int lru_rd = -1;
    for (int w = 0; w < SAMPLED_CACHE_WAYS; w++) {
      const auto& line = sampled_set[static_cast<std::size_t>(w)];
      if (!line.valid) {
        lru_way = w;
        lru_rd = inf_rd + 1;
        continue;
      }

      int sample = time_elapsed(current_timestamp[set_idx], line.timestamp);
      if (sample > inf_rd) {
        lru_way = w;
        lru_rd = inf_rd + 1;
        detrain(sampled_cache_index, w);
      } else if (sample > lru_rd) {
        lru_way = w;
        lru_rd = sample;
      }
    }
    detrain(sampled_cache_index, lru_way);

    for (auto& line : sampled_set) {
      if (!line.valid) {
        line.valid = true;
        line.signature = pc;
        line.tag = sampled_cache_tag;
        line.timestamp = current_timestamp[set_idx];
        break;
      }
    }

    current_timestamp[set_idx] = increment_timestamp(current_timestamp[set_idx]);
  }

  if (etr_clock[set_idx] == GRANULARITY) {
    for (long w = 0; w < NUM_WAY; w++) {
      if (w != way && std::abs(get_etr(set, w)) < inf_etr)
        get_etr(set, w)--;
    }
    etr_clock[set_idx] = 0;
  }
  etr_clock[set_idx]++;

  // A bypassed fill arrives with way == NUM_WAY: nothing was inserted.
  if (way < NUM_WAY) {
    auto signature = static_cast<uint32_t>(pc);
    if (auto it = rdp.find(signature); it == rdp.end())
      get_etr(set, way) = (NUM_CPUS == 1) ? 0 : inf_etr;
    else if (it->second > max_rd)
      get_etr(set, way) = inf_etr;
    else
      get_etr(set, way) = it->second / GRANULARITY;
  }
}

void @MODULE@::replacement_final_stats() { fmt::print("Mockingjay bypassed fills: {}\n", bypassed_fills); }