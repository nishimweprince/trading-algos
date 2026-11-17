// Local-validator acceptance helper. Mainnet use is read-only manifest capture; simulation is LOCAL ONLY.
#include <cstdio>
#include <fstream>
#include <optional>
#include <sstream>
#include <set>
#include <simdjson.h>
#include "app/bundle_sim.hpp"
#include "core/config.hpp"
#include "core/crypto.hpp"
#include "core/rpc.hpp"
#include "core/programs.hpp"
#include "feed/resync.hpp"
#include "flash/kamino.hpp"
#include "state/accounts.hpp"
#include "state/discovery.hpp"
#include "tx/builders.hpp"
#include "tx/dex_ix.hpp"
using namespace arb;
int main(int argc, char** argv) {
 try {
  std::string pool_s, holder_s, executor_s, url, manifest, flash_s;
  uint64_t amount=1000000, minimum=0;
  for(int i=1;i+1<argc;i+=2) {
   std::string a=argv[i],v=argv[i+1];
   if(a=="--pool")pool_s=v;else if(a=="--holder")holder_s=v;else if(a=="--executor")executor_s=v;
   else if(a=="--rpc")url=v;else if(a=="--manifest")manifest=v;else if(a=="--flash")flash_s=v;
   else if(a=="--amount")amount=std::stoull(v);else if(a=="--min-out")minimum=std::stoull(v);
   else throw std::runtime_error("unknown argument");
  }
  if(pool_s.empty()||holder_s.empty()||executor_s.empty()) throw std::runtime_error("requires --pool --holder --executor; optional --manifest FILE, --rpc URL, --amount RAW, --min-out RAW, --flash KAMINO_RESERVE");
  crypto_init(); Config cfg;
  if(url.empty())url=resolve_rpc_url(cfg);
  Rpc rpc(url,30000);Pubkey pool=parse_pubkey(pool_s),owner=parse_pubkey(holder_s),exec=parse_pubkey(executor_s);
  auto account=rpc.account(pool,"processed");if(!account)throw std::runtime_error("missing pool");
  auto def=state::pool_def_from_account({pool,account->owner,account->data},{});if(!def)throw std::runtime_error("unsupported pool");
  if(def->mint_a!=mint::WSOL&&def->mint_b!=mint::WSOL)throw std::runtime_error("probe requires wSOL pair");
  StateStore store;store.add_pool(*def);feed::resync(rpc,store,store.subscribed_keys(),true);auto p=store.pool(0);
  bool d=p->mint_a==mint::WSOL; Pubkey other=d?p->mint_b:p->mint_a;
  engine::Cycle cycle;cycle.n=2;cycle.base=mint::WSOL;
  cycle.legs[0]={0,d,mint::WSOL,other};cycle.legs[1]={0,!d,other,mint::WSOL};
  std::shared_ptr<const PoolState> legs[2]={p,p};tx::IxList ixs;tx::set_compute_unit_limit(ixs,1400000);
  Pubkey intermediate=tx::wallet_ata(owner,other,*p),base=tx::wallet_ata(owner,mint::WSOL,*p);
  auto tp=other==p->mint_a?p->mint_a_info:p->mint_b_info;
  ixs.add(prog::ATA).acc(owner,true,true).rw(intermediate).ro(owner).ro(other).ro(prog::SYSTEM).ro(tp?tp->token_program:prog::TOKEN).u8(1);
  if (p->kind==DexKind::PumpSwap) {
    auto volume=rpc.account(tx::dexix::pump_user_volume_accumulator(owner),"processed");
    if (!volume) tx::dexix::pump_init_user_volume(ixs,owner);
    else {
      auto body=rpc.call("getMinimumBalanceForRentExemption","["+std::to_string(volume->data.size())+"]");
      simdjson::dom::parser parser;uint64_t minimum=parser.parse(body)["result"];
      if (minimum>volume->lamports)tx::dexix::pump_fund_user_volume(ixs,owner,minimum-volume->lamports);
    }
  }
  // --flash: wrap the executor in a Kamino flash borrow/repay of the wSOL reserve (spec §7.3 layout, no tip).
  std::optional<flash::FlashReserve> fr;
  uint8_t borrow_index=0;
  if(!flash_s.empty()) {
    Pubkey rk=parse_pubkey(flash_s);auto ra=rpc.account(rk,"processed");if(!ra)throw std::runtime_error("missing reserve");
    auto r=state::decode_kamino_reserve(ra->data.data(),ra->data.size(),ra->owner);if(!r)throw std::runtime_error(std::string("bad reserve: ")+r.error().what);
    fr=flash::make_reserve(rk,*r,0);
    if(fr->liquidity_mint!=mint::WSOL)throw std::runtime_error("--flash reserve must lend wSOL");
    if(!fr->enabled)throw std::runtime_error("flash loans disabled on reserve");
    borrow_index=ixs.n;flash::borrow_ix(ixs,*fr,owner,base,amount);
  }
  auto encoded=tx::execute_route_ix(ixs,exec,owner,cycle,legs,amount,minimum);
  if(!encoded)throw std::runtime_error(encoded.error().what);
  if(fr)flash::repay_ix(ixs,*fr,owner,base,amount,borrow_index);
  auto wire=app::unsigned_tx_b64(owner,ixs);
  if(wire.empty())throw std::runtime_error("static executor route exceeds packet size");
  if(!manifest.empty()) {
   std::set<Pubkey> keys;for(uint8_t i=0;i<ixs.n;i++){keys.insert(ixs.ixs[i].program);for(uint8_t j=0;j<ixs.ixs[i].n_accounts;j++)keys.insert(ixs.ixs[i].accounts[j].key);}
   for(auto k:store.subscribed_keys())keys.insert(k);
   std::ofstream f(manifest);f<<"{\"pool\":\""<<pool_s<<"\",\"holder\":\""<<holder_s<<"\",\"executor\":\""<<executor_s<<"\",\"base_account\":\""<<b58(base)<<"\",\"kind\":\""<<dex_name(p->kind)<<"\",\"accounts\":[";
   bool first=true;for(auto k:keys){f<<(first?"":",")<<"\""<<b58(k)<<"\"";first=false;}f<<"],\"transaction\":\""<<wire<<"\"}\n";
   std::printf("manifest captured %zu public accounts; kind=%s\n",keys.size(),dex_name(p->kind));return 0;
  }
  if(url.rfind("http://127.0.0.1:",0)!=0&&url.rfind("http://localhost:",0)!=0)throw std::runtime_error("executor simulations restricted to localhost");
  auto before=rpc.account(base,"processed");uint64_t pre=0;if(before&&before->data.size()>=72)std::memcpy(&pre,before->data.data()+64,8);
  auto sim=rpc.simulate_full(wire,{base},0,"processed");uint64_t post=0;if(!sim.accounts.empty()&&sim.accounts[0]&&sim.accounts[0]->data.size()>=72)std::memcpy(&post,sim.accounts[0]->data.data()+64,8);
  uint64_t out=post+amount>=pre?post+amount-pre:0;
  // With --flash the repay (amount + fee) runs after the executor's min_out check, so the whole-transaction delta
  // is the executor's measured output minus the flash fee: add it back to report what the guard compared.
  if(fr&&sim.ok){auto fee=flash::flash_loan_fee(amount,fr->flash_loan_fee_sf);if(!fee)throw std::runtime_error("flash fee");out+=*fee;}
  std::printf("{\"ok\":%s,\"final_out\":%llu,\"units\":%llu,\"min_out\":%llu,\"min_out_error\":%s}\n",sim.ok?"true":"false",(unsigned long long)out,(unsigned long long)sim.units,(unsigned long long)minimum,sim.err.find("6002")!=std::string::npos?"true":"false");
  if(!sim.ok){std::fprintf(stderr,"%s\n",sim.err.c_str());for(auto& l:sim.logs)std::fprintf(stderr,"%s\n",l.c_str());}
  return sim.ok?0:1;
 }catch(const std::exception&e){std::fprintf(stderr,"executor-probe: %s\n",e.what());return 1;}
}
