//+------------------------------------------------------------------+
//| HlaPublisher: streams ticks, symbol specs and account state of   |
//| every bridge symbol to hlarb. Never trades.                      |
//| Install: MQL5/Services, add it in Navigator > Services, start.   |
//+------------------------------------------------------------------+
#property service
#property copyright "trading-algos"
#property version   "1.00"
#property description "hlarb tick and state publisher (no trading)"

#include "HlaBridge.mqh"

input string BridgeName     = "Local\\hla_bridge_v1";
input int    SpecIntervalMs = 1000;  // specs, sessions, tick value, account
input int    IdleSleepMs    = 0;     // 0 spins on one core for the lowest tick latency

void PublishSpecs(const string &names[])
  {
   for(int i = 0; i < ArraySize(names); i++)
     {
      const string s = names[i];
      hla_publish_spec(i,
                       SymbolInfoDouble(s, SYMBOL_TRADE_CONTRACT_SIZE),
                       SymbolInfoDouble(s, SYMBOL_VOLUME_STEP),
                       SymbolInfoDouble(s, SYMBOL_VOLUME_MIN),
                       SymbolInfoDouble(s, SYMBOL_VOLUME_MAX),
                       SymbolInfoDouble(s, SYMBOL_TRADE_TICK_SIZE),
                       SymbolInfoDouble(s, SYMBOL_TRADE_TICK_VALUE),
                       (int)SymbolInfoInteger(s, SYMBOL_DIGITS),
                       (int)SymbolInfoInteger(s, SYMBOL_TRADE_MODE),
                       (int)SymbolInfoInteger(s, SYMBOL_FILLING_MODE),
                       HlaSessionOpen(s) ? 1 : 0);
     }
  }

void PublishAccount()
  {
   const bool trading = TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) != 0 &&
                        AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) != 0 &&
                        AccountInfoInteger(ACCOUNT_TRADE_EXPERT) != 0;
   hla_account(AccountInfoInteger(ACCOUNT_LOGIN),
               AccountInfoDouble(ACCOUNT_BALANCE),
               AccountInfoDouble(ACCOUNT_EQUITY),
               AccountInfoDouble(ACCOUNT_MARGIN_FREE),
               (int)TerminalInfoInteger(TERMINAL_CONNECTED),
               trading ? 1 : 0,
               (int)AccountInfoInteger(ACCOUNT_MARGIN_MODE));
  }

void OnStart()
  {
   if(!HlaWaitOpen(BridgeName, "HlaPublisher"))
      return;
   string names[];
   MqlTick last[];
   long generation = -1;
   ulong next_spec = 0;
   while(!IsStopped())
     {
      const long current = hla_generation();
      if(current == 0)
        {
         Sleep(100);  // hlarb is restarting and has cleared the bridge
         continue;
        }
      if(current != generation)
        {
         generation = current;
         HlaLoadSymbols(names, "HlaPublisher");
         ArrayResize(last, ArraySize(names));
         for(int i = 0; i < ArraySize(last); i++)
            last[i].time_msc = 0;
         next_spec = 0;
        }
      bool sent = false;
      for(int i = 0; i < ArraySize(names); i++)
        {
         MqlTick tick;
         if(!SymbolInfoTick(names[i], tick) || tick.bid <= 0 || tick.ask <= 0)
            continue;
         if(tick.time_msc == last[i].time_msc && tick.bid == last[i].bid && tick.ask == last[i].ask)
            continue;
         last[i] = tick;
         hla_push_tick(i, tick.bid, tick.ask, tick.time_msc);
         sent = true;
        }
      hla_heartbeat(0);
      const ulong now = GetMicrosecondCount();
      if(now >= next_spec)
        {
         PublishSpecs(names);
         PublishAccount();
         next_spec = now + (ulong)SpecIntervalMs * 1000;
        }
      if(!sent && IdleSleepMs > 0)
         Sleep(IdleSleepMs);
     }
   hla_close();
  }
//+------------------------------------------------------------------+
