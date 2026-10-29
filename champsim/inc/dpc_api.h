/*
 *    Copyright 2025 The ChampSim Contributors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#ifndef __DPC_API_H__
#define __DPC_API_H__

#include <cstdint>

//--------------------------------------------------------------------//
// Get instanteneous DRAM bandwdith estimate.
//
// This function (1) internally measures the current DRAM bandwidth usage
// across all DRAM channels, (2) nomalizes the utilization w.r.t the peak
// DRAM bandwidth, and (3) returns a 4-bit quantized normalized utilization.
// For example, a return value of 3 denotes the current DRAM bandwidth
// usage is between 3x(100/16) = 18.75% to 4x(100/16) = 25% of the peak.
//--------------------------------------------------------------------//
uint8_t get_dram_bw();

//--------------------------------------------------------------------//
// Returns the number of instructions retired
// since the beginning of the simulation.
//
// Can be helpful in measuring core's performance and adapt
// prefetching strategy accordingly.
// Note that, the value returned by this function includes warmup instructions.
//--------------------------------------------------------------------//
long long get_retired_insts(uint8_t cpu_id);

#endif /* __DPC_API_H__ */
