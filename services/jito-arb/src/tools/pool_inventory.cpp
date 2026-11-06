// Read-only deployment capacity gate. No RPC, wallet files, or network connections.
#include <cstdio>
#include <fstream>
#include <sstream>
#include <map>
#include "app/pools_file.hpp"
#include "core/config.hpp"
#include "core/crypto.hpp"
#include "feed/subscription.hpp"
using namespace arb;
int main(int argc,char** argv) {
  try {
    if(argc!=3)throw std::runtime_error("usage: pool-inventory CONFIG POOLS_JSON");
    crypto_init();auto cfg=load_config(argv[1]);std::ifstream file(argv[2]);
    if(!file)throw std::runtime_error("cannot read pools");
    std::stringstream text;text<<file.rdbuf();app::PoolsFile pools;std::string error;
    if(!app::from_json(text.str(),pools,&error))throw std::runtime_error(error);
    StateStore store;std::map<std::string,size_t> counts;
    for(auto& p:pools.pools){store.add_pool(p);++counts[dex_name(p.kind)];}
    auto keys=store.subscribed_keys();
    auto plan=feed::plan_subscriptions(keys,{},cfg.helius.max_grpc_connections,cfg.helius.shard_size);
    std::printf("{\"pools\":%zu,\"required_pool_keys\":%zu,\"pool_streams\":%zu,\"total_streams_with_wallet\":%zu,\"overflow\":%zu,\"by_dex\":{",pools.pools.size(),keys.size(),plan.pool_shards.size(),plan.pool_shards.size()+1,plan.overflow);
    bool first=true;for(auto&[name,n]:counts){std::printf("%s\"%s\":%zu",first?"":",",name.c_str(),n);first=false;}
    std::puts("}}");return plan.overflow||plan.pool_shards.size()+1>10?1:0;
  }catch(const std::exception& e){std::fprintf(stderr,"pool-inventory: %s\n",e.what());return 1;}
}
