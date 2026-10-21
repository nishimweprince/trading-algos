//+------------------------------------------------------------------+
//| HlaBridge.mqh: imports from hla_bridge.dll (MQL5/Libraries) and  |
//| helpers shared by the HlaPublisher and HlaExecutor services.     |
//| The C ABI is declared in include/hla/mt5_bridge.hpp.             |
//+------------------------------------------------------------------+

#import "hla_bridge.dll"
int  hla_open(string name);
long hla_generation();
int  hla_symbol_count();
int  hla_symbol_name(int index, ushort &buffer[], int capacity);
int  hla_publish_spec(int index, double contract_size, double volume_step, double volume_min, double volume_max,
                      double tick_size, double tick_value, int digits, int trade_mode, int filling_mode,
                      int session_open);
int  hla_push_tick(int index, double bid, double ask, long time_msc);
int  hla_pop_order(long &request_id, int &index, int &side, double &volume, int &deviation_points);
int  hla_push_txn(long request_id, int index, int side, int status, int retcode, double volume, double price,
                  double commission, long deal, long order);
void hla_account(long login, double balance, double equity, double margin_free, int connected,
                 int trade_allowed, int margin_mode);
void hla_position(int index, double net_lots);
void hla_positions_done();
void hla_heartbeat(int role);
long hla_engine_age_ms();
void hla_close();
#import

// Txn status codes (bridge::TxnStatus).
#define HLA_FILLED   1
#define HLA_PARTIAL  2
#define HLA_REJECTED 3
#define HLA_ERROR    4

// Blocks until hlarb has created the bridge (or the service is stopped).
bool HlaWaitOpen(const string bridge_name, const string who)
  {
   bool told = false;
   while(!IsStopped())
     {
      const int result = hla_open(bridge_name);
      if(result == 1)
         return true;
      if(result < 0)
        {
         Print(who, ": bridge version mismatch, rebuild hla_bridge.dll and hlarb from one commit");
         return false;
        }
      if(!told)
         Print(who, ": waiting for hlarb to create ", bridge_name);
      told = true;
      Sleep(500);
     }
   return false;
  }

// Reads the symbol list that hlarb wrote into the bridge and selects each in Market Watch.
void HlaLoadSymbols(string &names[], const string who)
  {
   const int n = hla_symbol_count();
   ArrayResize(names, n);
   ushort buffer[64];
   for(int i = 0; i < n; i++)
     {
      hla_symbol_name(i, buffer, 64);
      names[i] = ShortArrayToString(buffer);
      if(!SymbolSelect(names[i], true))
         Print(who, ": cannot select ", names[i], " (error ", GetLastError(), ")");
     }
   Print(who, ": ", n, " symbols, generation ", hla_generation());
  }

// True while one of today's trade sessions for `symbol` covers the current server time.
bool HlaSessionOpen(const string symbol)
  {
   MqlDateTime now;
   TimeToStruct(TimeTradeServer(), now);
   const long seconds = now.hour * 3600 + now.min * 60 + now.sec;
   datetime from, to;
   for(uint i = 0; SymbolInfoSessionTrade(symbol, (ENUM_DAY_OF_WEEK)now.day_of_week, i, from, to); i++)
     {
      if(seconds >= (long)from && seconds < (long)to)
         return true;
     }
   return false;
  }
//+------------------------------------------------------------------+
