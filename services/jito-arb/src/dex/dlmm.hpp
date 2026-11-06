#pragma once
#include "dex/quoter.hpp"
namespace arb::dex::dlmm {
constexpr u128 ONE = u128(1) << 64;
int32_t array_index(int32_t bin);
bool initialized(const state::DlmmPair&, const state::DlmmBitmapExtension*, int32_t index);
bool next_array(const PoolState&, int32_t from, bool swap_for_y, int32_t& out);
Result<u128> price(int32_t id, uint16_t step);
Result<state::DlmmPair> fee_context(const PoolState&);
Result<uint64_t> fee_rate(state::DlmmPair&, int32_t id);
u128 mid(const PoolState&, bool swap_for_y);
u128 spot(const PoolState&, bool swap_for_y);
uint64_t depth(const PoolState&, bool swap_for_y);
}
