// Replays the hook calls ChampSim made on the ported Mockingjay through the
// authors' unmodified CRC2 code, and checks that every victim and bypass decision
// the port made is the one the original makes.
//
// Event mapping, the CRC2 contract ("called on every cache hit and cache fill"):
//   U ... hit=1  -> llc_update_replacement_state(hit=1)
//   U ... hit=0  -> ignored (a miss; its fill follows)
//   V            -> llc_find_victim on a full set; expected way remembered per set
//   F            -> compare with the remembered decision, then update(hit=0)

#include <cinttypes>
#include <cstdio>
#include <vector>

#include "cache.h"

int main(int argc, char** argv)
{
  if (argc != 2) {
    std::fprintf(stderr, "usage: %s events.log\n", argv[0]);
    return 2;
  }
  std::FILE* in = std::fopen(argv[1], "r");
  if (!in) {
    std::perror(argv[1]);
    return 2;
  }

  CACHE crc2;
  crc2.llc_initialize_replacement();
  BLOCK full_set[LLC_WAY];
  for (auto& b : full_set)
    b.valid = true;

  std::vector<long> expected(LLC_SET, -1);
  unsigned long long lines = 0, hits = 0, misses = 0, fills = 0, prefetch_events = 0;
  unsigned long long decisions = 0, matches = 0, mismatches = 0, retries = 0, bypass_port = 0, bypass_crc2 = 0;

  char line[256];
  while (std::fgets(line, sizeof line, in)) {
    ++lines;
    unsigned cpu, type, hit;
    long set, way;
    uint64_t addr, ip;

    if (std::sscanf(line, "V %u %ld %" SCNx64 " %u", &cpu, &set, &ip, &type) == 4) {
      if (expected.at(static_cast<std::size_t>(set)) >= 0)
        ++retries; // a fill that could not proceed and was retried
      expected[static_cast<std::size_t>(set)] = crc2.llc_find_victim(cpu, 0, static_cast<uint32_t>(set), full_set, ip, 0, type);
    } else if (std::sscanf(line, "U %u %ld %ld %" SCNx64 " %" SCNx64 " %u %u", &cpu, &set, &way, &addr, &ip, &type, &hit) == 7) {
      prefetch_events += (type == PREFETCH);
      if (hit) {
        ++hits;
        crc2.llc_update_replacement_state(cpu, static_cast<uint32_t>(set), static_cast<uint32_t>(way), addr, ip, 0, type, 1);
      } else {
        ++misses;
      }
    } else if (std::sscanf(line, "F %u %ld %ld %" SCNx64 " %" SCNx64 " %u", &cpu, &set, &way, &addr, &ip, &type) == 6) {
      ++fills;
      prefetch_events += (type == PREFETCH);
      long& want = expected.at(static_cast<std::size_t>(set));
      if (want >= 0) {
        ++decisions;
        bypass_port += (way == LLC_WAY);
        bypass_crc2 += (want == LLC_WAY);
        if (want == way) {
          ++matches;
        } else {
          if (mismatches < 10)
            std::fprintf(stderr, "line %llu: set %ld: port chose way %ld, original chose way %ld\n", lines, set, way, want);
          ++mismatches;
        }
        want = -1;
      }
      crc2.llc_update_replacement_state(cpu, static_cast<uint32_t>(set), static_cast<uint32_t>(way), addr, ip, 0, type, 0);
    } else {
      std::fprintf(stderr, "line %llu: unparsed: %s", lines, line);
      return 1;
    }
  }

  std::printf("events %llu  hits %llu  misses %llu  fills %llu  prefetch-type events %llu\n", lines, hits, misses, fills, prefetch_events);
  std::printf("victim decisions compared %llu  identical %llu  different %llu  (retried searches %llu)\n", decisions, matches, mismatches, retries);
  std::printf("bypasses: port %llu  original %llu\n", bypass_port, bypass_crc2);
  return (mismatches == 0 && decisions > 0) ? 0 : 1;
}
