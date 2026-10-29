#ifndef MJ_RECORDER_H
#define MJ_RECORDER_H

// Test-only replacement module: logs every hook call ChampSim makes on the LLC
// to the file named by MJ_EVENTS (default ./mj_events.log). List it BEFORE the
// module under test, so that module's find_victim result is the one used
// (ChampSim returns the last module's victim).

#include <cinttypes>
#include <cstdio>
#include <cstdlib>

#include "cache.h"
#include "modules.h"

struct mj_recorder : public champsim::modules::replacement {
  static std::FILE* log_file()
  {
    static std::FILE* f = std::fopen(std::getenv("MJ_EVENTS") ? std::getenv("MJ_EVENTS") : "mj_events.log", "w");
    return f;
  }

  explicit mj_recorder(CACHE* cache) : replacement(cache) { log_file(); }

  long find_victim(uint32_t triggering_cpu, uint64_t, long set, const champsim::cache_block*, champsim::address ip, champsim::address, access_type type)
  {
    std::fprintf(log_file(), "V %u %ld %" PRIx64 " %u\n", triggering_cpu, set, ip.to<uint64_t>(), static_cast<unsigned>(type));
    return 0;
  }

  void update_replacement_state(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip, champsim::address,
                                access_type type, uint8_t hit)
  {
    std::fprintf(log_file(), "U %u %ld %ld %" PRIx64 " %" PRIx64 " %u %u\n", triggering_cpu, set, way, full_addr.to<uint64_t>(), ip.to<uint64_t>(),
                 static_cast<unsigned>(type), static_cast<unsigned>(hit));
  }

  void replacement_cache_fill(uint32_t triggering_cpu, long set, long way, champsim::address full_addr, champsim::address ip, champsim::address,
                              access_type type)
  {
    std::fprintf(log_file(), "F %u %ld %ld %" PRIx64 " %" PRIx64 " %u\n", triggering_cpu, set, way, full_addr.to<uint64_t>(), ip.to<uint64_t>(),
                 static_cast<unsigned>(type));
  }

  void replacement_final_stats() { std::fflush(log_file()); }
};

#endif
