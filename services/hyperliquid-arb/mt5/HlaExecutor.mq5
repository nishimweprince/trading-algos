//+------------------------------------------------------------------+
//| HlaExecutor: executes hlarb hedge orders and reports net         |
//| positions. Synchronous OrderSend, so each result carries the     |
//| exact fill, price and commission. Orders older than the engine's |
//| HLA_MT5_ORDER_MAX_AGE_MS are dropped by the DLL, never executed. |
//| Install: MQL5/Services; enable Algo Trading for the service.     |
//+------------------------------------------------------------------+
#property service
#property copyright "trading-algos"
#property version   "1.10"
#property description "hlarb hedge executor"

#include "HlaBridge.mqh"

input string BridgeName     = "Local\\hla_bridge_v1";
input long   Magic          = 7750001;  // positions with this magic are hlarb's
input int    PositionScanMs = 250;
input int    IdleSleepMs    = 0;        // 0 spins on one core for the lowest hedge latency
input int    DealLookupMs   = 100;      // wait this long for a deal the result did not name

// Where a hedge price came from, sent in the txn status high byte (engine journals it as px_src).
#define HLA_PX_RESULT  0
#define HLA_PX_HISTORY 1
#define HLA_PX_REQUEST 2

ENUM_ORDER_TYPE_FILLING Filling(const string symbol)
  {
   const long modes = SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   if((modes & SYMBOL_FILLING_IOC) == SYMBOL_FILLING_IOC)
      return ORDER_FILLING_IOC;
   if((modes & SYMBOL_FILLING_FOK) == SYMBOL_FILLING_FOK)
      return ORDER_FILLING_FOK;
   return ORDER_FILLING_RETURN;
  }

double NormalizeLots(const string symbol, const double lots)
  {
   const double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   return step > 0 ? MathRound(lots / step) * step : lots;
  }

// Some brokers return DONE with deal 0 and price 0: the deal lands in history a moment later.
bool FindDeal(const ulong order, ulong &deal, double &price, double &volume, double &commission)
  {
   const ulong deadline = GetMicrosecondCount() + (ulong)DealLookupMs * 1000;
   do
     {
      if(HistorySelect(TimeCurrent() - 60, TimeCurrent() + 60))
         for(int i = HistoryDealsTotal() - 1; i >= 0; i--)
           {
            const ulong ticket = HistoryDealGetTicket(i);
            if(ticket == 0 || (ulong)HistoryDealGetInteger(ticket, DEAL_ORDER) != order)
               continue;
            deal       = ticket;
            price      = HistoryDealGetDouble(ticket, DEAL_PRICE);
            volume     = HistoryDealGetDouble(ticket, DEAL_VOLUME);
            commission = HistoryDealGetDouble(ticket, DEAL_COMMISSION);
            return price > 0;
           }
      Sleep(5);
     }
   while(GetMicrosecondCount() < deadline);
   return false;
  }

// One market deal; accumulates the filled volume, notional and commission.
bool Send(const string symbol, const int side, const double lots, const int deviation, const ulong position,
          const long id, uint &retcode, double &filled, double &notional, double &commission, ulong &deal,
          ulong &order, int &px_src)
  {
   MqlTradeRequest request;
   MqlTradeResult result;
   ZeroMemory(request);
   ZeroMemory(result);
   request.action       = TRADE_ACTION_DEAL;
   request.symbol       = symbol;
   request.volume       = lots;
   request.type         = side == 0 ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   request.price        = side == 0 ? SymbolInfoDouble(symbol, SYMBOL_ASK) : SymbolInfoDouble(symbol, SYMBOL_BID);
   request.deviation    = (ulong)deviation;
   request.magic        = (ulong)Magic;
   request.comment      = "hla " + IntegerToString(id);
   request.type_filling = Filling(symbol);
   if(position > 0)
      request.position = position;
   if(!OrderSend(request, result) && result.retcode == 0)
      result.retcode = (uint)GetLastError();
   retcode = result.retcode;
   if(result.retcode != TRADE_RETCODE_DONE && result.retcode != TRADE_RETCODE_DONE_PARTIAL)
      return false;
   deal  = result.deal;
   order = result.order;
   double price = result.price;
   double volume = result.volume;
   int source = HLA_PX_RESULT;
   if(result.deal > 0 && HistoryDealSelect(result.deal))
     {
      commission += HistoryDealGetDouble(result.deal, DEAL_COMMISSION);
      if(price <= 0)
        {
         price = HistoryDealGetDouble(result.deal, DEAL_PRICE);
         source = HLA_PX_HISTORY;
        }
     }
   else if(result.order > 0)
     {
      ulong found = 0;
      double found_price = 0, found_volume = 0, found_commission = 0;
      if(FindDeal(result.order, found, found_price, found_volume, found_commission))
        {
         deal = found;
         commission += found_commission;
         if(price <= 0)
           {
            price = found_price;
            source = HLA_PX_HISTORY;
           }
         if(volume <= 0)
            volume = found_volume;
        }
     }
   if(price <= 0)
     {
      price = request.price;  // last resort: the touch we sent at
      source = HLA_PX_REQUEST;
     }
   if(source > px_src)
      px_src = source;
   filled   += volume;
   notional += volume * price;
   return true;
  }

void Execute(const string &names[], const long id, const int index, const int side, const double volume,
             const int deviation)
  {
   if(index < 0 || index >= ArraySize(names))
     {
      hla_push_txn(id, index, side, HLA_ERROR, 0, 0, 0, 0, 0, 0);
      return;
     }
   const string symbol = names[index];
   uint retcode = 0;
   double filled = 0, notional = 0, commission = 0;
   ulong deal = 0, order = 0;
   int px_src = HLA_PX_RESULT;
   // Hedging accounts: close our opposite positions first so the net moves as on a netting account.
   if(AccountInfoInteger(ACCOUNT_MARGIN_MODE) == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
     {
      for(int i = PositionsTotal() - 1; i >= 0 && volume - filled > 1e-9; i--)
        {
         const ulong ticket = PositionGetTicket(i);
         if(ticket == 0 || PositionGetString(POSITION_SYMBOL) != symbol || PositionGetInteger(POSITION_MAGIC) != Magic)
            continue;
         const long type = PositionGetInteger(POSITION_TYPE);
         if((side == 0 && type != POSITION_TYPE_SELL) || (side == 1 && type != POSITION_TYPE_BUY))
            continue;
         const double lots = NormalizeLots(symbol, MathMin(volume - filled, PositionGetDouble(POSITION_VOLUME)));
         if(lots <= 0 || !Send(symbol, side, lots, deviation, ticket, id, retcode, filled, notional, commission, deal, order,
                              px_src))
            break;
        }
     }
   const double remaining = NormalizeLots(symbol, volume - filled);
   if(remaining > 1e-9)
      Send(symbol, side, remaining, deviation, 0, id, retcode, filled, notional, commission, deal, order, px_src);
   int status = HLA_REJECTED;
   if(filled >= volume - 1e-9)
      status = HLA_FILLED;
   else if(filled > 0)
      status = HLA_PARTIAL;
   else if(retcode == 0)
      status = HLA_ERROR;
   hla_push_txn(id, index, side, status | (px_src << 8), (int)retcode, filled, filled > 0 ? notional / filled : 0,
                commission, (long)deal, (long)order);
  }

void ScanPositions(const string &names[])
  {
   for(int s = 0; s < ArraySize(names); s++)
     {
      double net = 0;
      for(int i = PositionsTotal() - 1; i >= 0; i--)
        {
         const ulong ticket = PositionGetTicket(i);
         if(ticket == 0 || PositionGetString(POSITION_SYMBOL) != names[s] || PositionGetInteger(POSITION_MAGIC) != Magic)
            continue;
         const double lots = PositionGetDouble(POSITION_VOLUME);
         net += PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? lots : -lots;
        }
      hla_position(s, net);
     }
   hla_positions_done();
  }

void OnStart()
  {
   if(!HlaWaitOpen(BridgeName, "HlaExecutor"))
      return;
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))
      Print("HlaExecutor: algo trading is not allowed for this service; hedges will be rejected");
   string names[];
   long generation = -1;
   ulong next_scan = 0;
   while(!IsStopped())
     {
      const long current = hla_generation();
      if(current == 0)
        {
         Sleep(100);
         continue;
        }
      if(current != generation)
        {
         generation = current;
         HlaLoadSymbols(names, "HlaExecutor");
         next_scan = 0;
        }
      long id = 0;
      int index = 0, side = 0, deviation = 0;
      double volume = 0;
      if(hla_pop_order(id, index, side, volume, deviation) == 1)
        {
         Execute(names, id, index, side, volume, deviation);
         ScanPositions(names);
         continue;
        }
      hla_heartbeat(1);
      const ulong now = GetMicrosecondCount();
      if(now >= next_scan)
        {
         ScanPositions(names);
         next_scan = now + (ulong)PositionScanMs * 1000;
        }
      if(IdleSleepMs > 0)
         Sleep(IdleSleepMs);
     }
   hla_close();
  }
//+------------------------------------------------------------------+
