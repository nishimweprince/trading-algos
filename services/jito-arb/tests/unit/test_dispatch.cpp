#include <cstring>
#include <stdexcept>

#include "core/programs.hpp"
#include "dex/quoter.hpp"
#include "engine/sizer.hpp"
#include "state/state_store.hpp"
#include "test.hpp"
#include "tx/dex_ix.hpp"

using namespace arb;

TEST(dispatch_unsupported_kinds_never_use_whirlpool_or_raydium_fallback) {
  dex::RaydiumV4Quoter ray;
  dex::WhirlpoolQuoter wp;
  const dex::IQuoter* quoters[] = {&ray, &wp};
  engine::Quoters qs;
  for (const dex::IQuoter* q : quoters) qs.by_kind[uint8_t(q->kind())] = q;
  for (DexKind kind : {DexKind::Clmm, DexKind::Dlmm, DexKind(6), DexKind(255)}) {
    CHECK(dex_program(kind) == nullptr);
    CHECK(qs.get(kind) == nullptr);
    PoolState p;
    p.kind = kind;
    for (bool direction : {false, true}) {
      bool accounts_threw = false, data_threw = false, program_threw = false;
      try { (void)tx::dexix::swap_accounts(p, direction, {}); }
      catch (const std::invalid_argument&) { accounts_threw = true; }
      try { (void)tx::dexix::swap_data(kind, direction, 1000, 1); }
      catch (const std::invalid_argument&) { data_threw = true; }
      try { (void)tx::dexix::program_of(kind); }
      catch (const std::invalid_argument&) { program_threw = true; }
      CHECK(accounts_threw && data_threw && program_threw);
      CHECK_EQ(dex::spot_rate_x64(p, direction), u128(0));
      CHECK_EQ(dex::input_depth(p, direction), uint64_t(0));
      for (const dex::IQuoter* q : quoters) {
        auto result = q->quote_exact_in(p, direction, 1000);
        CHECK(!result && result.error().code == Errc::unsupported);
      }
    }
    StateStore store;
    PoolDef def;
    def.kind = kind;
    bool threw = false;
    try { store.add_pool(def); }
    catch (const std::invalid_argument&) { threw = true; }
    CHECK(threw && store.pool_count() == 0);
  }
}

TEST(dispatch_current_supported_swap_encodings_still_select_their_program) {
  for (DexKind kind : {DexKind::RaydiumV4, DexKind::Whirlpool}) {
    CHECK(dex_program(kind) != nullptr);
    CHECK(tx::dexix::program_of(kind) == *dex_program(kind));
    for (bool direction : {false, true}) {
      auto bytes = tx::dexix::swap_data(kind, direction, 123456, 789);
      const size_t expected = kind == DexKind::RaydiumV4 ? 17 : 42;
      CHECK_EQ(bytes.size(), expected);
      uint64_t amount = 0, minimum = 0;
      size_t offset = kind == DexKind::RaydiumV4 ? 1 : 8;
      std::memcpy(&amount, bytes.data() + offset, 8);
      std::memcpy(&minimum, bytes.data() + offset + 8, 8);
      CHECK_EQ(amount, uint64_t(123456));
      CHECK_EQ(minimum, uint64_t(789));
    }
  }
}
