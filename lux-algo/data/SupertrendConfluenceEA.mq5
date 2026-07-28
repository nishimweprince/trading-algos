//+------------------------------------------------------------------+
//|                                     SupertrendConfluenceEA.mq5   |
//|                                                                  |
//|  Supertrend crossover entry, gated by a configurable confluence   |
//|  of seven trend overlays and two counter-trend vetoes.            |
//|                                                                  |
//|  The indicator maths are hand-written (not taken from iATR/iMA)   |
//|  so that every series reproduces the Pine Script semantics of the |
//|  original chart study (see ../file.txt) and stays bit-compatible  |
//|  with the Python reference implementation in ../src/lux_algo/.    |
//|  Built-in MT5 indicators seed their averages differently and      |
//|  would silently diverge.                                          |
//|                                                                  |
//|  Signals are evaluated on bar close only: the EA reads the bar    |
//|  that has just completed, never the forming bar, so nothing       |
//|  repaints.                                                        |
//+------------------------------------------------------------------+
#property copyright "Copyright 2026"
#property link      "https://www.mql5.com"
#property version   "1.00"
#property description "Supertrend crossover entry filtered by a multi-overlay confluence gate."
#property description "Overlays: Range Filter, SuperIchi, TBO, Smart Trail, Heikin-Ashi bias, MACD regime, Parabolic SAR."
#property description "Counter-trend vetoes: exhaustion take-profit points and RSI reversals."
#property description "Risk management: percent-equity or fixed sizing, break-even, trailing stop, spread/session/daily-loss guards."
#property description "Signals are evaluated on bar close only - no repainting."

#include <Trade\Trade.mqh>
#include <Trade\SymbolInfo.mqh>
#include <Trade\PositionInfo.mqh>

//+------------------------------------------------------------------+
//| Enumerations                                                     |
//+------------------------------------------------------------------+
enum ENUM_CONFLUENCE_MODE
  {
   CONFLUENCE_UNANIMOUS = 0,  // Unanimous - every enabled overlay must agree
   CONFLUENCE_THRESHOLD = 1,  // Threshold - at least N overlays must agree
   CONFLUENCE_OFF       = 2   // Off - raw Supertrend signal, no gating
  };

enum ENUM_SIZING_MODE
  {
   SIZING_FIXED_LOT      = 0, // Fixed lot
   SIZING_PERCENT_EQUITY = 1  // Percent of equity risked per trade
  };

enum ENUM_TRAIL_MODE
  {
   TRAIL_OFF   = 0, // Off
   TRAIL_ATR   = 1, // ATR distance from price
   TRAIL_SMART = 2  // Smart Trail overlay line
  };

//+------------------------------------------------------------------+
//| Inputs                                                           |
//+------------------------------------------------------------------+
input group "=== Signal ==="
input ENUM_TIMEFRAMES InpTimeframe        = PERIOD_CURRENT; // Strategy timeframe
input double          InpSensitivity      = 5.5;            // Supertrend sensitivity (ATR factor)
input int             InpAtrLen           = 11;             // Supertrend ATR length
input int             InpSmaLen           = 13;             // Confirmation SMA length
input int             InpHistoryBars      = 1500;           // Bars used per recalculation
input int             InpMinWarmupBars    = 300;            // Minimum bars before trading starts

input group "=== Confluence gate ==="
input ENUM_CONFLUENCE_MODE InpConfluenceMode      = CONFLUENCE_THRESHOLD; // Confluence mode
input int                  InpConfluenceThreshold = 3;                    // Overlays required (Threshold mode; 0 = all)
input bool                 InpUseRangeFilter      = true;                 // Overlay: Range Filter
input bool                 InpUseSuperIchi        = true;                 // Overlay: SuperIchi
input bool                 InpUseTbo              = true;                 // Overlay: TBO (EMA 20/40)
input bool                 InpUseSmartTrail       = true;                 // Overlay: Smart Trail
input bool                 InpUseHaBias           = false;                // Overlay: Heikin-Ashi market bias
input bool                 InpUseMacdColor        = false;                // Overlay: MACD regime
input bool                 InpUsePsar             = false;                // Overlay: Parabolic SAR

input group "=== Counter-trend vetoes ==="
input bool InpVetoTpPoints = false; // Veto entry on opposing exhaustion TP point
input bool InpVetoReversals = false; // Veto entry on opposing RSI reversal

input group "=== Stops and targets ==="
input bool   InpUseStopLoss    = true;  // Attach a stop loss
input bool   InpUseTakeProfit  = true;  // Attach a take profit
input double InpRiskReward     = 2.0;   // Take profit as a multiple of the stop distance
input bool   InpUseHardTargets = false; // Use fixed money targets instead of Supertrend/RR
input double InpHardSlMoney    = 25.0;  // Fixed stop loss (account currency, fixed-lot only)
input double InpHardTpMoney    = 40.0;  // Fixed take profit (account currency, fixed-lot only)
input double InpMinStopPoints  = 0.0;   // Minimum stop distance in points (0 = broker minimum)

input group "=== Position sizing ==="
input ENUM_SIZING_MODE InpSizingMode  = SIZING_PERCENT_EQUITY; // Sizing mode
input double           InpFixedLot    = 0.10;                  // Fixed lot size
input double           InpRiskPercent = 1.0;                   // Risk per trade (% of equity)
input double           InpMaxLot      = 0.0;                   // Hard lot cap (0 = broker maximum)

input group "=== Trade management ==="
input ENUM_TRAIL_MODE InpTrailMode         = TRAIL_OFF; // Trailing stop mode
input double          InpTrailAtrMult      = 2.0;       // Trailing distance (ATR multiples)
input int             InpTrailAtrPeriod    = 14;        // Trailing ATR period
input double          InpBreakEvenAtR      = 0.0;       // Move stop to break-even at N x risk (0 = off)
input double          InpBreakEvenOffsetPt = 5.0;       // Break-even offset in points
input bool            InpCloseOnOpposite   = false;     // Close open trades on an opposite signal

input group "=== Risk guards ==="
input int    InpMaxPositions     = 1;    // Maximum concurrent positions
input bool   InpOnePerDirection  = true; // At most one position per direction
input int    InpMinBarsBetween   = 0;    // Minimum bars between entries
input int    InpMaxSpreadPoints  = 30;   // Maximum spread in points (0 = no limit)
input double InpMaxDailyLossPct  = 0.0;  // Daily loss limit, % of day-start balance (0 = off)

input group "=== Session filter ==="
input bool InpUseSessionFilter = false; // Restrict trading to an hour window
input int  InpSessionStartHour = 7;     // Session start hour (server time, 0-23)
input int  InpSessionEndHour   = 20;    // Session end hour (server time, 0-23)
input bool InpTradeMonday      = true;  // Trade on Monday
input bool InpTradeTuesday     = true;  // Trade on Tuesday
input bool InpTradeWednesday   = true;  // Trade on Wednesday
input bool InpTradeThursday    = true;  // Trade on Thursday
input bool InpTradeFriday      = true;  // Trade on Friday
input bool InpTradeSaturday    = false; // Trade on Saturday
input bool InpTradeSunday      = false; // Trade on Sunday

input group "=== Execution ==="
input long   InpMagicNumber     = 78650001;             // Magic number
input int    InpSlippagePoints  = 20;                   // Maximum deviation in points
input string InpOrderComment    = "SupertrendConfl";    // Order comment
input int    InpOrderRetries    = 3;                    // Retries on requote / price change

input group "=== Diagnostics ==="
input bool InpVerboseLog = false; // Log every evaluated bar
input int  InpDebugBars  = 0;     // Print indicator values for the last N bars (0 = off)

//+------------------------------------------------------------------+
//| Constants and globals                                            |
//+------------------------------------------------------------------+
#define NA_VALUE   DBL_MAX
#define OV_COUNT   7

#define OV_RANGE_FILTER 0
#define OV_SUPERICHI    1
#define OV_TBO          2
#define OV_SMART_TRAIL  3
#define OV_HA_BIAS      4
#define OV_MACD         5
#define OV_PSAR         6

CTrade         g_trade;
CSymbolInfo    g_symbol;
CPositionInfo  g_position;

ENUM_TIMEFRAMES g_tf            = PERIOD_CURRENT;
datetime        g_lastBarTime   = 0;
bool            g_firstBarSeen  = false;
datetime        g_lastEntryBar  = 0;
double          g_dayStartBalance = 0.0;
int             g_dayStamp      = -1;
bool            g_warmupLogged  = false;

// Price window, oldest -> newest, ending on the last closed bar.
MqlRates g_rates[];
double   g_open[];
double   g_high[];
double   g_low[];
double   g_close[];
int      g_n = 0;

// Cached values from the most recent bar-close evaluation.
double g_lastSupertrend = 0.0;
double g_lastSmartTrail = 0.0;
double g_lastAtr        = 0.0;
int    g_overlayDir[OV_COUNT];
bool   g_overlayOn[OV_COUNT];
string g_overlayName[OV_COUNT];

// Open-position bookkeeping (initial risk per ticket, break-even state).
ulong  g_trkTicket[];
double g_trkRisk[];
bool   g_trkBreakEven[];

//+------------------------------------------------------------------+
//| Small helpers                                                    |
//+------------------------------------------------------------------+
bool IsNA(const double value)
  {
   if(!MathIsValidNumber(value))
      return(true);
   return(value >= NA_VALUE || value <= -NA_VALUE);
  }

//+------------------------------------------------------------------+
//| Simple moving average. NA until 'length' samples exist.          |
//| Port of indicators.sma().                                        |
//+------------------------------------------------------------------+
void SeriesSma(const double &src[], const int n, const int length, double &out[])
  {
   ArrayResize(out, n);
   double running = 0.0;
   for(int i = 0; i < n; i++)
     {
      running += src[i];
      if(i >= length)
         running -= src[i - length];
      out[i] = (i >= length - 1) ? running / length : NA_VALUE;
     }
  }

//+------------------------------------------------------------------+
//| Wilder moving average (Pine ta.rma): alpha = 1/length, seeded    |
//| with the SMA of the first 'length' samples.                      |
//| Port of indicators.rma().                                        |
//+------------------------------------------------------------------+
void SeriesRma(const double &src[], const int n, const int length, double &out[])
  {
   ArrayResize(out, n);
   double seed[];
   SeriesSma(src, n, length, seed);

   const double alpha = 1.0 / length;
   bool   seeded = false;
   double prev   = 0.0;

   for(int i = 0; i < n; i++)
     {
      if(!seeded)
        {
         out[i] = seed[i];
         if(!IsNA(seed[i]))
           {
            prev   = seed[i];
            seeded = true;
           }
        }
      else
        {
         prev   = alpha * src[i] + (1.0 - alpha) * prev;
         out[i] = prev;
        }
     }
  }

//+------------------------------------------------------------------+
//| Exponential moving average seeded with the first sample.         |
//| Port of indicators.ema(). NOTE: Pine's ta.ema seeds with an SMA;  |
//| this seeding is inherited from the Python reference so both      |
//| engines agree. It converges well inside the warmup window.       |
//+------------------------------------------------------------------+
void SeriesEma(const double &src[], const int n, const int length, double &out[])
  {
   ArrayResize(out, n);
   const double alpha = 2.0 / (length + 1.0);
   double prev = 0.0;
   for(int i = 0; i < n; i++)
     {
      prev   = (i == 0) ? src[i] : alpha * src[i] + (1.0 - alpha) * prev;
      out[i] = prev;
     }
  }

//+------------------------------------------------------------------+
//| Pine Wild_ma: wild := wild[1] + (src - wild[1]) / length,        |
//| seeded from zero. Port of indicators.wilder_ma().                |
//+------------------------------------------------------------------+
void SeriesWilderMa(const double &src[], const int n, const int length, double &out[])
  {
   ArrayResize(out, n);
   double prev = 0.0;
   for(int i = 0; i < n; i++)
     {
      prev   = prev + (src[i] - prev) / length;
      out[i] = prev;
     }
  }

//+------------------------------------------------------------------+
//| True range over the loaded window. Port of indicators.true_range()|
//+------------------------------------------------------------------+
void SeriesTrueRange(double &out[])
  {
   ArrayResize(out, g_n);
   for(int i = 0; i < g_n; i++)
     {
      if(i == 0)
        {
         out[i] = g_high[i] - g_low[i];
         continue;
        }
      const double prevClose = g_close[i - 1];
      double value = g_high[i] - g_low[i];
      value = MathMax(value, MathAbs(g_high[i] - prevClose));
      value = MathMax(value, MathAbs(g_low[i]  - prevClose));
      out[i] = value;
     }
  }

//+------------------------------------------------------------------+
//| Average true range (Pine ta.atr = rma(tr, length)).              |
//+------------------------------------------------------------------+
void SeriesAtr(const int length, double &out[])
  {
   double tr[];
   SeriesTrueRange(tr);
   SeriesRma(tr, g_n, length, out);
  }

//+------------------------------------------------------------------+
//| Pine ta.crossover / ta.crossunder at bar i.                      |
//+------------------------------------------------------------------+
bool SeriesCrossOver(const double &a[], const double &b[], const int i)
  {
   if(i < 1)
      return(false);
   if(IsNA(a[i]) || IsNA(b[i]) || IsNA(a[i - 1]) || IsNA(b[i - 1]))
      return(false);
   return(a[i] > b[i] && a[i - 1] <= b[i - 1]);
  }

bool SeriesCrossUnder(const double &a[], const double &b[], const int i)
  {
   if(i < 1)
      return(false);
   if(IsNA(a[i]) || IsNA(b[i]) || IsNA(a[i - 1]) || IsNA(b[i - 1]))
      return(false);
   return(a[i] < b[i] && a[i - 1] >= b[i - 1]);
  }

bool SeriesCross(const double &a[], const double &b[], const int i)
  {
   return(SeriesCrossOver(a, b, i) || SeriesCrossUnder(a, b, i));
  }

//+------------------------------------------------------------------+
//| Supertrend. Port of indicators.supertrend(), which mirrors the    |
//| supertrend() function in file.txt (lines 25-43).                  |
//| direction: -1 = up-trend (line below price), 1 = down-trend.      |
//+------------------------------------------------------------------+
void SeriesSupertrend(const double factor, const int atrLen, double &st[], int &dir[])
  {
   double atrValues[];
   SeriesAtr(atrLen, atrValues);

   ArrayResize(st, g_n);
   ArrayResize(dir, g_n);

   double prevLower = 0.0;   // Pine nz(lowerBand[1]) -> 0 on the first bar
   double prevUpper = 0.0;
   double prevSt    = 0.0;
   bool   hasPrevSt = false;

   for(int i = 0; i < g_n; i++)
     {
      if(IsNA(atrValues[i]))
        {
         st[i]     = NA_VALUE;
         dir[i]    = 0;
         hasPrevSt = false;
         continue;
        }

      const double src   = g_close[i];
      const double upper = src + factor * atrValues[i];
      const double lower = src - factor * atrValues[i];
      const bool   hasPrevClose = (i > 0);
      const double prevClose    = hasPrevClose ? g_close[i - 1] : 0.0;

      double lowerBand = prevLower;
      if(lower > prevLower || (hasPrevClose && prevClose < prevLower))
         lowerBand = lower;

      double upperBand = prevUpper;
      if(upper < prevUpper || (hasPrevClose && prevClose > prevUpper))
         upperBand = upper;

      const bool prevAtrNA = (i == 0) ? true : IsNA(atrValues[i - 1]);

      int direction;
      if(prevAtrNA || !hasPrevSt)
         direction = 1;
      else
         if(prevSt == prevUpper)
            direction = (src > upperBand) ? -1 : 1;
         else
            direction = (src < lowerBand) ? 1 : -1;

      const double value = (direction == -1) ? lowerBand : upperBand;
      st[i]  = value;
      dir[i] = direction;

      prevLower = lowerBand;
      prevUpper = upperBand;
      prevSt    = value;
      hasPrevSt = true;
     }
  }

//+------------------------------------------------------------------+
//| Overlay: Range Filter (file.txt 202-325; Type 2, close source,    |
//| qty 2.618, period 14, smoothing 27). Port of overlays.range_filter|
//+------------------------------------------------------------------+
void OverlayRangeFilterDir(const int rngPer, const double rngQty, const int smoothPer, int &fdir[])
  {
   ArrayResize(fdir, g_n);
   if(g_n <= 0)
      return;

   double acSrc[];
   ArrayResize(acSrc, g_n);
   acSrc[0] = 0.0;
   for(int i = 1; i < g_n; i++)
      acSrc[i] = MathAbs(g_close[i] - g_close[i - 1]);

   double ac[];
   SeriesEma(acSrc, g_n, rngPer, ac);

   double rng[];
   ArrayResize(rng, g_n);
   for(int i = 0; i < g_n; i++)
      rng[i] = rngQty * ac[i];

   double r[];
   SeriesEma(rng, g_n, smoothPer, r);

   double filt[];
   ArrayResize(filt, g_n);
   double prevFilt = g_close[0];

   for(int i = 0; i < g_n; i++)
     {
      double current = prevFilt;
      if(r[i] > 0.0)
        {
         if(g_close[i] >= prevFilt + r[i])
            current = prevFilt + MathFloor(MathAbs(g_close[i] - prevFilt) / r[i]) * r[i];
         if(g_close[i] <= prevFilt - r[i])
            current = prevFilt - MathFloor(MathAbs(g_close[i] - prevFilt) / r[i]) * r[i];
        }
      filt[i] = current;
      prevFilt = current;
     }

   fdir[0] = 0;
   int currentDir = 0;
   for(int i = 1; i < g_n; i++)
     {
      if(filt[i] > filt[i - 1])
         currentDir = 1;
      else
         if(filt[i] < filt[i - 1])
            currentDir = -1;
      fdir[i] = currentDir;
     }
  }

//+------------------------------------------------------------------+
//| SuperIchi average band (file.txt avg(); overlays._ichi_avg).      |
//+------------------------------------------------------------------+
void IchiAvg(const double &srcc[], const int length, const double mult, double &out[])
  {
   ArrayResize(out, g_n);
   if(g_n <= 0)
      return;

   double atrValues[];
   SeriesAtr(length, atrValues);

   double upper[], lower[], spt[], mx[], mn[];
   int    os[];
   ArrayResize(upper, g_n);
   ArrayResize(lower, g_n);
   ArrayResize(spt,   g_n);
   ArrayResize(mx,    g_n);
   ArrayResize(mn,    g_n);
   ArrayResize(os,    g_n);

   for(int i = 0; i < g_n; i++)
     {
      const double band = IsNA(atrValues[i]) ? 0.0 : atrValues[i] * mult;
      const double hl2  = (g_high[i] + g_low[i]) / 2.0;
      const double up   = hl2 + band;
      const double dn   = hl2 - band;

      if(i == 0)
        {
         upper[i] = up;
         lower[i] = dn;
         os[i]    = 0;
         spt[i]   = upper[i];
         mx[i]    = srcc[i];
         mn[i]    = srcc[i];
         out[i]   = srcc[i];
         continue;
        }

      upper[i] = (srcc[i - 1] < upper[i - 1]) ? MathMin(up, upper[i - 1]) : up;
      lower[i] = (srcc[i - 1] > lower[i - 1]) ? MathMax(dn, lower[i - 1]) : dn;

      if(srcc[i] > upper[i])
         os[i] = 1;
      else
         if(srcc[i] < lower[i])
            os[i] = 0;
         else
            os[i] = os[i - 1];

      spt[i] = (os[i] == 1) ? lower[i] : upper[i];

      if(SeriesCross(srcc, spt, i))
        {
         mx[i] = MathMax(srcc[i], mx[i - 1]);
         mn[i] = MathMin(srcc[i], mn[i - 1]);
        }
      else
        {
         mx[i] = (os[i] == 1) ? MathMax(srcc[i], mx[i - 1]) : spt[i];
         mn[i] = (os[i] == 0) ? MathMin(srcc[i], mn[i - 1]) : spt[i];
        }

      out[i] = (mx[i] + mn[i]) / 2.0;
     }
  }

//+------------------------------------------------------------------+
//| Overlay: SuperIchi direction - tenkan vs kijun (file.txt 328-380).|
//+------------------------------------------------------------------+
void OverlaySuperIchiDir(int &dir[])
  {
   ArrayResize(dir, g_n);
   double tenkan[], kijun[];
   IchiAvg(g_close, 6, 2.0, tenkan);
   IchiAvg(g_close, 5, 3.0, kijun);
   for(int i = 0; i < g_n; i++)
      dir[i] = (tenkan[i] > kijun[i]) ? 1 : ((tenkan[i] < kijun[i]) ? -1 : 0);
  }

//+------------------------------------------------------------------+
//| Overlay: TBO - EMA(20) vs EMA(40) (file.txt 382-429, defaults).   |
//+------------------------------------------------------------------+
void OverlayTboDir(int &dir[])
  {
   ArrayResize(dir, g_n);
   double fast[], medium[];
   SeriesEma(g_close, g_n, 20, fast);
   SeriesEma(g_close, g_n, 40, medium);
   for(int i = 0; i < g_n; i++)
      dir[i] = (fast[i] > medium[i]) ? 1 : ((fast[i] < medium[i]) ? -1 : 0);
  }

//+------------------------------------------------------------------+
//| Overlay: Smart Trail (file.txt 431-501); modified true range,     |
//| ATR period 13, factor 4. Returns the trend state and the smoothed |
//| trailing-stop line that the indicator plots.                      |
//+------------------------------------------------------------------+
void OverlaySmartTrail(const int atrPeriod, const double atrFactor, const int smoothing,
                       int &trend[], double &trailSmoothed[])
  {
   ArrayResize(trend, g_n);
   ArrayResize(trailSmoothed, g_n);
   if(g_n <= 0)
      return;

   double hl[];
   ArrayResize(hl, g_n);
   for(int i = 0; i < g_n; i++)
      hl[i] = g_high[i] - g_low[i];

   double smaHl[];
   SeriesSma(hl, g_n, atrPeriod, smaHl);

   double tr[];
   ArrayResize(tr, g_n);
   for(int i = 0; i < g_n; i++)
     {
      if(i == 0)
        {
         tr[i] = g_high[i] - g_low[i];
         continue;
        }
      const double prevClose = g_close[i - 1];
      const double prevHigh  = g_high[i - 1];
      const double prevLow   = g_low[i - 1];
      const double base = IsNA(smaHl[i]) ? (g_high[i] - g_low[i]) : 1.5 * smaHl[i];
      const double hiLo = MathMin(g_high[i] - g_low[i], base);
      const double hRef = (g_low[i] <= prevHigh)
                          ? (g_high[i] - prevClose)
                          : (g_high[i] - prevClose - 0.5 * (g_low[i] - prevHigh));
      const double lRef = (g_high[i] >= prevLow)
                          ? (prevClose - g_low[i])
                          : (prevClose - g_low[i] - 0.5 * (prevLow - g_high[i]));
      tr[i] = MathMax(hiLo, MathMax(hRef, lRef));
     }

   double wild[];
   SeriesWilderMa(tr, g_n, atrPeriod, wild);

   double trendUp[], trendDown[], trail[];
   ArrayResize(trendUp, g_n);
   ArrayResize(trendDown, g_n);
   ArrayResize(trail, g_n);

   for(int i = 0; i < g_n; i++)
     {
      const double loss = atrFactor * wild[i];
      const double up   = g_close[i] - loss;
      const double dn   = g_close[i] + loss;

      if(i == 0)
        {
         trendUp[i]   = up;
         trendDown[i] = dn;
         trend[i]     = 1;
        }
      else
        {
         trendUp[i]   = (g_close[i - 1] > trendUp[i - 1])   ? MathMax(up, trendUp[i - 1])   : up;
         trendDown[i] = (g_close[i - 1] < trendDown[i - 1]) ? MathMin(dn, trendDown[i - 1]) : dn;

         if(g_close[i] > trendDown[i - 1])
            trend[i] = 1;
         else
            if(g_close[i] < trendUp[i - 1])
               trend[i] = -1;
            else
               trend[i] = trend[i - 1];
        }

      trail[i] = (trend[i] == 1) ? trendUp[i] : trendDown[i];
     }

   // The chart plots ta.sma(trail, smoothing) as the trailing stop (file.txt:498).
   double smoothed[];
   SeriesSma(trail, g_n, smoothing, smoothed);
   for(int i = 0; i < g_n; i++)
      trailSmoothed[i] = IsNA(smoothed[i]) ? trail[i] : smoothed[i];
  }

//+------------------------------------------------------------------+
//| Overlay: Heikin-Ashi market bias (file.txt 148-200).             |
//+------------------------------------------------------------------+
void OverlayHaBiasDir(const int haLen, const int haLen2, int &dir[])
  {
   ArrayResize(dir, g_n);
   if(g_n <= 0)
      return;

   double o[], c[], h[], l[];
   SeriesEma(g_open,  g_n, haLen, o);
   SeriesEma(g_close, g_n, haLen, c);
   SeriesEma(g_high,  g_n, haLen, h);
   SeriesEma(g_low,   g_n, haLen, l);

   double haClose[], xHaOpen[], haOpen[];
   ArrayResize(haClose, g_n);
   ArrayResize(xHaOpen, g_n);
   ArrayResize(haOpen,  g_n);

   for(int i = 0; i < g_n; i++)
     {
      haClose[i] = (o[i] + h[i] + l[i] + c[i]) / 4.0;
      xHaOpen[i] = (o[i] + c[i]) / 2.0;
     }
   for(int i = 0; i < g_n; i++)
      haOpen[i] = (i == 0) ? xHaOpen[i] : (xHaOpen[i - 1] + haClose[i - 1]) / 2.0;

   double o2[], c2[];
   SeriesEma(haOpen,  g_n, haLen2, o2);
   SeriesEma(haClose, g_n, haLen2, c2);

   for(int i = 0; i < g_n; i++)
      dir[i] = (c2[i] > o2[i]) ? 1 : ((c2[i] < o2[i]) ? -1 : 0);
  }

//+------------------------------------------------------------------+
//| Overlay: MACD candle-colour regime (file.txt 680-738); 12/26/9.  |
//+------------------------------------------------------------------+
void OverlayMacdDir(int &dir[])
  {
   ArrayResize(dir, g_n);
   if(g_n <= 0)
      return;

   double fast[], slow[], macdLine[], signal[];
   SeriesEma(g_close, g_n, 12, fast);
   SeriesEma(g_close, g_n, 26, slow);

   ArrayResize(macdLine, g_n);
   for(int i = 0; i < g_n; i++)
      macdLine[i] = fast[i] - slow[i];

   SeriesEma(macdLine, g_n, 9, signal);

   for(int i = 0; i < g_n; i++)
     {
      const double hist = macdLine[i] - signal[i];
      if(macdLine[i] > 0.0 && hist > 0.0)
         dir[i] = 1;
      else
         if(macdLine[i] < 0.0 && hist < 0.0)
            dir[i] = -1;
         else
            dir[i] = 0;
     }
  }

//+------------------------------------------------------------------+
//| Overlay: Parabolic SAR direction (file.txt 66, 78).              |
//| Port of indicators.psar() / overlays.psar_dir().                 |
//+------------------------------------------------------------------+
void OverlayPsarDir(const double afStart, const double afInc, const double afMax, int &dir[])
  {
   ArrayResize(dir, g_n);
   for(int i = 0; i < g_n; i++)
      dir[i] = 0;
   if(g_n < 2)
      return;

   bool   bull = g_close[1] > g_close[0];
   double af   = afStart;
   double ep   = bull ? g_high[0] : g_low[0];
   double sar  = bull ? g_low[0]  : g_high[0];

   for(int i = 1; i < g_n; i++)
     {
      sar = sar + af * (ep - sar);

      if(bull)
        {
         sar = MathMin(sar, g_low[i - 1]);
         sar = MathMin(sar, (i >= 2) ? g_low[i - 2] : g_low[i - 1]);
         if(g_high[i] > ep)
           {
            ep = g_high[i];
            af = MathMin(af + afInc, afMax);
           }
         if(g_low[i] < sar)
           {
            bull = false;
            sar  = ep;
            ep   = g_low[i];
            af   = afStart;
           }
        }
      else
        {
         sar = MathMax(sar, g_high[i - 1]);
         sar = MathMax(sar, (i >= 2) ? g_high[i - 2] : g_high[i - 1]);
         if(g_low[i] < ep)
           {
            ep = g_low[i];
            af = MathMin(af + afInc, afMax);
           }
         if(g_high[i] > sar)
           {
            bull = true;
            sar  = ep;
            ep   = g_high[i];
            af   = afStart;
           }
        }

      const double ocAvg = (g_open[i] + g_close[i]) / 2.0;
      dir[i] = (sar < ocAvg) ? 1 : -1;
     }
  }

//+------------------------------------------------------------------+
//| Veto source: exhaustion take-profit points (Pine lele(),          |
//| file.txt 98-146). -1 = major sell TP, 1 = major buy TP.           |
//+------------------------------------------------------------------+
void SeriesTpPoints(const int qual, const int length, int &out[])
  {
   ArrayResize(out, g_n);
   int bIndex = 0;
   int sIndex = 0;

   for(int i = 0; i < g_n; i++)
     {
      int ret = 0;

      if(i >= 4)
        {
         if(g_close[i] > g_close[i - 4])
            bIndex++;
         if(g_close[i] < g_close[i - 4])
            sIndex++;
        }

      if(i >= length - 1)
        {
         double highest = g_high[i - length + 1];
         double lowest  = g_low[i - length + 1];
         for(int k = i - length + 2; k <= i; k++)
           {
            highest = MathMax(highest, g_high[k]);
            lowest  = MathMin(lowest,  g_low[k]);
           }
         if(bIndex > qual && g_close[i] < g_open[i] && g_high[i] >= highest)
           {
            bIndex = 0;
            ret    = -1;
           }
         if(sIndex > qual && g_close[i] > g_open[i] && g_low[i] <= lowest)
           {
            sIndex = 0;
            ret    = 1;
           }
        }

      out[i] = ret;
     }
  }

//+------------------------------------------------------------------+
//| Veto source: RSI reversal signals (file.txt 503-517).            |
//+------------------------------------------------------------------+
void SeriesReversals(const int length, const double overbought, const double oversold,
                     bool &revUp[], bool &revDown[])
  {
   ArrayResize(revUp, g_n);
   ArrayResize(revDown, g_n);
   for(int i = 0; i < g_n; i++)
     {
      revUp[i]   = false;
      revDown[i] = false;
     }
   if(g_n <= 1)
      return;

   double gains[], losses[];
   ArrayResize(gains, g_n);
   ArrayResize(losses, g_n);
   gains[0]  = 0.0;
   losses[0] = 0.0;
   for(int i = 1; i < g_n; i++)
     {
      const double delta = g_close[i] - g_close[i - 1];
      gains[i]  = MathMax(delta, 0.0);
      losses[i] = MathMax(-delta, 0.0);
     }

   double upward[], downward[];
   SeriesRma(gains,  g_n, length, upward);
   SeriesRma(losses, g_n, length, downward);

   double source[], obLevel[], osLevel[];
   ArrayResize(source, g_n);
   ArrayResize(obLevel, g_n);
   ArrayResize(osLevel, g_n);

   for(int i = 0; i < g_n; i++)
     {
      obLevel[i] = overbought;
      osLevel[i] = oversold;

      if(IsNA(upward[i]) || IsNA(downward[i]))
        {
         source[i] = NA_VALUE;
         continue;
        }
      if(downward[i] == 0.0)
         source[i] = 100.0;
      else
         if(upward[i] == 0.0)
            source[i] = 0.0;
         else
            source[i] = 100.0 - (100.0 / (1.0 + upward[i] / downward[i]));
     }

   for(int i = 0; i < g_n; i++)
     {
      revUp[i]   = SeriesCrossOver(source, osLevel, i);
      revDown[i] = SeriesCrossUnder(source, obLevel, i);
     }
  }

//+------------------------------------------------------------------+
//| Load the price window ending on the last closed bar.             |
//+------------------------------------------------------------------+
bool LoadRates()
  {
   const int available = Bars(_Symbol, g_tf);
   if(available <= 2)
      return(false);

   const int count = (int)MathMin(InpHistoryBars, available - 1);
   if(count < 2)
      return(false);

   ArraySetAsSeries(g_rates, false);
   const int copied = CopyRates(_Symbol, g_tf, 1, count, g_rates);
   if(copied <= 0)
      return(false);

   g_n = copied;
   ArrayResize(g_open,  g_n);
   ArrayResize(g_high,  g_n);
   ArrayResize(g_low,   g_n);
   ArrayResize(g_close, g_n);

   for(int i = 0; i < g_n; i++)
     {
      g_open[i]  = g_rates[i].open;
      g_high[i]  = g_rates[i].high;
      g_low[i]   = g_rates[i].low;
      g_close[i] = g_rates[i].close;
     }
   return(true);
  }

//+------------------------------------------------------------------+
//| Compute every enabled overlay direction at bar index i.          |
//+------------------------------------------------------------------+
void ComputeOverlays(const int i)
  {
   for(int k = 0; k < OV_COUNT; k++)
      g_overlayDir[k] = 0;

   int dir[];

   if(g_overlayOn[OV_RANGE_FILTER])
     {
      OverlayRangeFilterDir(14, 2.618, 27, dir);
      g_overlayDir[OV_RANGE_FILTER] = dir[i];
     }

   if(g_overlayOn[OV_SUPERICHI])
     {
      OverlaySuperIchiDir(dir);
      g_overlayDir[OV_SUPERICHI] = dir[i];
     }

   if(g_overlayOn[OV_TBO])
     {
      OverlayTboDir(dir);
      g_overlayDir[OV_TBO] = dir[i];
     }

   // Smart Trail is computed whenever it is needed as a filter or as a trail source.
   if(g_overlayOn[OV_SMART_TRAIL] || InpTrailMode == TRAIL_SMART)
     {
      int    trend[];
      double trail[];
      OverlaySmartTrail(13, 4.0, 8, trend, trail);
      g_lastSmartTrail = trail[i];
      if(g_overlayOn[OV_SMART_TRAIL])
         g_overlayDir[OV_SMART_TRAIL] = trend[i];
     }

   if(g_overlayOn[OV_HA_BIAS])
     {
      OverlayHaBiasDir(100, 100, dir);
      g_overlayDir[OV_HA_BIAS] = dir[i];
     }

   if(g_overlayOn[OV_MACD])
     {
      OverlayMacdDir(dir);
      g_overlayDir[OV_MACD] = dir[i];
     }

   if(g_overlayOn[OV_PSAR])
     {
      OverlayPsarDir(0.02, 0.02, 0.2, dir);
      g_overlayDir[OV_PSAR] = dir[i];
     }
  }

//+------------------------------------------------------------------+
//| Confluence gate. want: 1 = long, -1 = short.                     |
//+------------------------------------------------------------------+
bool PassesConfluence(const int want, int &confirmations, int &enabledCount)
  {
   confirmations = 0;
   enabledCount  = 0;

   for(int k = 0; k < OV_COUNT; k++)
     {
      if(!g_overlayOn[k])
         continue;
      enabledCount++;
      if(g_overlayDir[k] == want)
         confirmations++;
     }

   if(InpConfluenceMode == CONFLUENCE_OFF || enabledCount == 0)
      return(true);

   if(InpConfluenceMode == CONFLUENCE_THRESHOLD)
     {
      const int needed = (InpConfluenceThreshold > 0)
                         ? (int)MathMin(InpConfluenceThreshold, enabledCount)
                         : enabledCount;
      return(confirmations >= needed);
     }

   return(confirmations == enabledCount);
  }

//+------------------------------------------------------------------+
//| Counter-trend vetoes at bar i.                                   |
//+------------------------------------------------------------------+
bool IsVetoed(const int i, const int want, string &reason)
  {
   reason = "";

   if(InpVetoTpPoints)
     {
      int tp[];
      SeriesTpPoints(13, 40, tp);
      if((want == 1 && tp[i] == -1) || (want == -1 && tp[i] == 1))
        {
         reason = "opposing exhaustion TP point";
         return(true);
        }
     }

   if(InpVetoReversals)
     {
      bool revUp[], revDown[];
      SeriesReversals(14, 75.0, 25.0, revUp, revDown);
      if((want == 1 && revDown[i]) || (want == -1 && revUp[i]))
        {
         reason = "opposing RSI reversal";
         return(true);
        }
     }

   return(false);
  }

//+------------------------------------------------------------------+
//| Position bookkeeping                                             |
//+------------------------------------------------------------------+
int TrackFind(const ulong ticket)
  {
   for(int i = 0; i < ArraySize(g_trkTicket); i++)
      if(g_trkTicket[i] == ticket)
         return(i);
   return(-1);
  }

void TrackAdd(const ulong ticket, const double risk)
  {
   if(TrackFind(ticket) >= 0)
      return;
   const int size = ArraySize(g_trkTicket);
   ArrayResize(g_trkTicket, size + 1);
   ArrayResize(g_trkRisk, size + 1);
   ArrayResize(g_trkBreakEven, size + 1);
   g_trkTicket[size]    = ticket;
   g_trkRisk[size]      = risk;
   g_trkBreakEven[size] = false;
  }

//+------------------------------------------------------------------+
//| Drop tracker entries whose positions are gone.                   |
//+------------------------------------------------------------------+
void TrackPrune()
  {
   const int size = ArraySize(g_trkTicket);
   if(size == 0)
      return;

   ulong  keepTicket[];
   double keepRisk[];
   bool   keepBe[];
   int    kept = 0;

   for(int i = 0; i < size; i++)
     {
      if(!PositionSelectByTicket(g_trkTicket[i]))
         continue;
      ArrayResize(keepTicket, kept + 1);
      ArrayResize(keepRisk, kept + 1);
      ArrayResize(keepBe, kept + 1);
      keepTicket[kept] = g_trkTicket[i];
      keepRisk[kept]   = g_trkRisk[i];
      keepBe[kept]     = g_trkBreakEven[i];
      kept++;
     }

   ArrayResize(g_trkTicket, kept);
   ArrayResize(g_trkRisk, kept);
   ArrayResize(g_trkBreakEven, kept);
   for(int i = 0; i < kept; i++)
     {
      g_trkTicket[i]    = keepTicket[i];
      g_trkRisk[i]      = keepRisk[i];
      g_trkBreakEven[i] = keepBe[i];
     }
  }

//+------------------------------------------------------------------+
//| Volume normalisation against broker limits and free margin.      |
//+------------------------------------------------------------------+
double NormalizeVolume(const double rawVolume)
  {
   double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double lotStep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);

   if(lotStep <= 0.0)
      lotStep = 0.01;
   if(minLot <= 0.0)
      minLot = lotStep;
   if(InpMaxLot > 0.0)
      maxLot = MathMin(maxLot, InpMaxLot);

   double volume = MathFloor(rawVolume / lotStep) * lotStep;
   volume = MathMax(volume, minLot);
   volume = MathMin(volume, maxLot);

   // Round to the step's precision so the request is not rejected on a
   // floating-point remainder.
   const int stepDigits = (int)MathMax(0.0, MathCeil(-MathLog10(lotStep)));
   return(NormalizeDouble(volume, stepDigits));
  }

//+------------------------------------------------------------------+
//| Reduce the volume until the required margin fits the account.    |
//+------------------------------------------------------------------+
double FitVolumeToMargin(const bool isBuy, double volume, const double price)
  {
   double lotStep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(lotStep <= 0.0)
      lotStep = 0.01;
   const double minLot = MathMax(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN), lotStep);
   const int    stepDigits = (int)MathMax(0.0, MathCeil(-MathLog10(lotStep)));
   const ENUM_ORDER_TYPE type = isBuy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   const double freeMargin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);

   for(int guard = 0; guard < 1000 && volume >= minLot; guard++)
     {
      double margin = 0.0;
      if(!OrderCalcMargin(type, _Symbol, volume, price, margin))
         return(volume); // Cannot evaluate; let the server decide.
      if(margin <= freeMargin * 0.95)
         return(volume);
      volume = NormalizeDouble(volume - lotStep, stepDigits);
     }

   return(0.0);
  }

//+------------------------------------------------------------------+
//| Minimum allowed distance between price and a stop, in prices.    |
//+------------------------------------------------------------------+
double MinStopDistance()
  {
   const double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   const long   stops  = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   const long   freeze = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_FREEZE_LEVEL);
   double distance = MathMax((double)stops, (double)freeze) * point;
   if(InpMinStopPoints > 0.0)
      distance = MathMax(distance, InpMinStopPoints * point);
   // Never allow a zero distance: a stop exactly at market is rejected.
   return(MathMax(distance, point));
  }

//+------------------------------------------------------------------+
//| Push SL/TP outside the broker's minimum distance.                |
//+------------------------------------------------------------------+
void ClampStops(const bool isBuy, const double price, double &sl, double &tp)
  {
   const int    digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   const double minDist = MinStopDistance();

   if(sl > 0.0)
     {
      if(isBuy)
         sl = MathMin(sl, price - minDist);
      else
         sl = MathMax(sl, price + minDist);
      sl = NormalizeDouble(sl, digits);
     }

   if(tp > 0.0)
     {
      if(isBuy)
         tp = MathMax(tp, price + minDist);
      else
         tp = MathMin(tp, price - minDist);
      tp = NormalizeDouble(tp, digits);
     }
  }

//+------------------------------------------------------------------+
//| Money value of one price unit for one lot.                       |
//+------------------------------------------------------------------+
double MoneyPerPriceUnitPerLot()
  {
   const double tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   const double tickSize  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickValue <= 0.0 || tickSize <= 0.0)
      return(0.0);
   return(tickValue / tickSize);
  }

//+------------------------------------------------------------------+
//| Guards                                                           |
//+------------------------------------------------------------------+
bool TradingAllowedNow(string &reason)
  {
   reason = "";

   if(TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) == 0)
     {
      reason = "algo trading disabled in the terminal";
      return(false);
     }
   if(MQLInfoInteger(MQL_TRADE_ALLOWED) == 0)
     {
      reason = "algo trading disabled for this expert";
      return(false);
     }
   if(AccountInfoInteger(ACCOUNT_TRADE_EXPERT) == 0)
     {
      reason = "expert trading disabled on the account";
      return(false);
     }
   if(AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) == 0)
     {
      reason = "trading disabled on the account";
      return(false);
     }

   const long tradeMode = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_MODE);
   if(tradeMode == SYMBOL_TRADE_MODE_DISABLED || tradeMode == SYMBOL_TRADE_MODE_CLOSEONLY)
     {
      reason = "symbol is not open for new trades";
      return(false);
     }

   return(true);
  }

bool SessionAllowsTrading(string &reason)
  {
   reason = "";

   MqlDateTime now;
   TimeToStruct(TimeCurrent(), now);

   bool dayEnabled = true;
   switch(now.day_of_week)
     {
      case 0:
         dayEnabled = InpTradeSunday;
         break;
      case 1:
         dayEnabled = InpTradeMonday;
         break;
      case 2:
         dayEnabled = InpTradeTuesday;
         break;
      case 3:
         dayEnabled = InpTradeWednesday;
         break;
      case 4:
         dayEnabled = InpTradeThursday;
         break;
      case 5:
         dayEnabled = InpTradeFriday;
         break;
      case 6:
         dayEnabled = InpTradeSaturday;
         break;
     }
   if(!dayEnabled)
     {
      reason = "day of week disabled";
      return(false);
     }

   if(!InpUseSessionFilter)
      return(true);

   const int hour = now.hour;
   bool inside = false;
   if(InpSessionStartHour <= InpSessionEndHour)
      inside = (hour >= InpSessionStartHour && hour < InpSessionEndHour);
   else
      inside = (hour >= InpSessionStartHour || hour < InpSessionEndHour); // window wraps midnight

   if(!inside)
     {
      reason = "outside the trading session";
      return(false);
     }
   return(true);
  }

bool SpreadAcceptable(string &reason)
  {
   reason = "";
   if(InpMaxSpreadPoints <= 0)
      return(true);

   const long spread = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
   if(spread > InpMaxSpreadPoints)
     {
      reason = StringFormat("spread %d exceeds the %d point limit", (int)spread, InpMaxSpreadPoints);
      return(false);
     }
   return(true);
  }

//+------------------------------------------------------------------+
//| Daily loss limit, keyed on the server day.                       |
//+------------------------------------------------------------------+
void RefreshDayStamp()
  {
   MqlDateTime now;
   TimeToStruct(TimeCurrent(), now);
   const int stamp = now.year * 10000 + now.mon * 100 + now.day;
   if(stamp != g_dayStamp)
     {
      g_dayStamp        = stamp;
      g_dayStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);
     }
  }

bool DailyLossLimitHit(string &reason)
  {
   reason = "";
   if(InpMaxDailyLossPct <= 0.0 || g_dayStartBalance <= 0.0)
      return(false);

   const double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   const double drawdown = g_dayStartBalance - equity;
   const double limit    = g_dayStartBalance * InpMaxDailyLossPct / 100.0;

   if(drawdown >= limit)
     {
      reason = StringFormat("daily loss limit reached (%.2f of %.2f)", drawdown, limit);
      return(true);
     }
   return(false);
  }

//+------------------------------------------------------------------+
//| Position counting for this expert on this symbol.                |
//+------------------------------------------------------------------+
int CountPositions(const int direction) // 0 = any, 1 = long, -1 = short
  {
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      if(!g_position.SelectByIndex(i))
         continue;
      if(g_position.Symbol() != _Symbol)
         continue;
      if(g_position.Magic() != InpMagicNumber)
         continue;

      if(direction == 0)
        {
         count++;
         continue;
        }
      const bool isLong = (g_position.PositionType() == POSITION_TYPE_BUY);
      if((direction == 1 && isLong) || (direction == -1 && !isLong))
         count++;
     }
   return(count);
  }

//+------------------------------------------------------------------+
//| Close every position held in the given direction.                |
//+------------------------------------------------------------------+
void CloseDirection(const int direction)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      if(!g_position.SelectByIndex(i))
         continue;
      if(g_position.Symbol() != _Symbol || g_position.Magic() != InpMagicNumber)
         continue;

      const bool isLong = (g_position.PositionType() == POSITION_TYPE_BUY);
      if((direction == 1 && !isLong) || (direction == -1 && isLong))
         continue;

      const ulong ticket = g_position.Ticket();
      if(!g_trade.PositionClose(ticket, (ulong)InpSlippagePoints))
         PrintFormat("Close failed for #%I64u: retcode %u (%s)",
                     ticket, g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription());
     }
  }

//+------------------------------------------------------------------+
//| Send a market order, retrying on transient price errors.         |
//+------------------------------------------------------------------+
bool SendMarketOrder(const bool isBuy, const double volume, double sl, double tp, const double risk)
  {
   for(int attempt = 0; attempt <= InpOrderRetries; attempt++)
     {
      if(!g_symbol.RefreshRates())
        {
         Print("Cannot refresh rates; skipping the entry.");
         return(false);
        }

      const double price = isBuy ? g_symbol.Ask() : g_symbol.Bid();
      if(price <= 0.0)
         return(false);

      double slOut = sl;
      double tpOut = tp;
      ClampStops(isBuy, price, slOut, tpOut);

      const bool ok = isBuy
                      ? g_trade.Buy(volume, _Symbol, price, slOut, tpOut, InpOrderComment)
                      : g_trade.Sell(volume, _Symbol, price, slOut, tpOut, InpOrderComment);

      const uint retcode = g_trade.ResultRetcode();

      if(ok && (retcode == TRADE_RETCODE_DONE || retcode == TRADE_RETCODE_PLACED ||
                retcode == TRADE_RETCODE_DONE_PARTIAL))
        {
         const ulong orderTicket = g_trade.ResultOrder();
         PrintFormat("%s %.2f %s at %s, SL %s, TP %s (order %I64u)",
                     isBuy ? "BUY" : "SELL", volume, _Symbol,
                     DoubleToString(g_trade.ResultPrice(), g_symbol.Digits()),
                     DoubleToString(slOut, g_symbol.Digits()),
                     DoubleToString(tpOut, g_symbol.Digits()), orderTicket);

         // In hedging mode the position ticket equals the opening order ticket;
         // in netting mode the symbol carries a single position. Register whichever
         // resolves so break-even and trailing know this trade's initial risk.
         if(PositionSelectByTicket(orderTicket))
            TrackAdd(orderTicket, risk);
         else
            if(PositionSelect(_Symbol))
               TrackAdd((ulong)PositionGetInteger(POSITION_TICKET), risk);
         return(true);
        }

      if(retcode == TRADE_RETCODE_REQUOTE || retcode == TRADE_RETCODE_PRICE_CHANGED ||
         retcode == TRADE_RETCODE_PRICE_OFF)
        {
         PrintFormat("Retryable order error %u (%s); attempt %d of %d.",
                     retcode, g_trade.ResultRetcodeDescription(), attempt + 1, InpOrderRetries + 1);
         continue;
        }

      if(retcode == TRADE_RETCODE_INVALID_STOPS)
        {
         PrintFormat("Broker rejected the stops (SL %s TP %s); entry skipped.",
                     DoubleToString(slOut, g_symbol.Digits()), DoubleToString(tpOut, g_symbol.Digits()));
         return(false);
        }

      PrintFormat("Order failed: retcode %u (%s).", retcode, g_trade.ResultRetcodeDescription());
      return(false);
     }

   Print("Order abandoned after exhausting the retry budget.");
   return(false);
  }

//+------------------------------------------------------------------+
//| Break-even and trailing stop maintenance, run on every tick.     |
//+------------------------------------------------------------------+
void ManageOpenPositions()
  {
   if(InpBreakEvenAtR <= 0.0 && InpTrailMode == TRAIL_OFF)
      return;
   if(!g_symbol.RefreshRates())
      return;

   const int    digits  = g_symbol.Digits();
   const double point   = g_symbol.Point();
   const double minDist = MinStopDistance();

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      if(!g_position.SelectByIndex(i))
         continue;
      if(g_position.Symbol() != _Symbol || g_position.Magic() != InpMagicNumber)
         continue;

      const ulong  ticket  = g_position.Ticket();
      const bool   isLong  = (g_position.PositionType() == POSITION_TYPE_BUY);
      const double entry   = g_position.PriceOpen();
      const double curSl   = g_position.StopLoss();
      const double curTp   = g_position.TakeProfit();
      const double market  = isLong ? g_symbol.Bid() : g_symbol.Ask();

      int idx = TrackFind(ticket);
      if(idx < 0)
        {
         // Position opened before a restart: reconstruct the risk from its stop,
         // and treat an at-entry stop as break-even already applied.
         const double recoveredRisk = (curSl > 0.0) ? MathAbs(entry - curSl) : 0.0;
         TrackAdd(ticket, recoveredRisk);
         idx = TrackFind(ticket);
         if(idx >= 0 && curSl > 0.0 && MathAbs(curSl - entry) <= point)
            g_trkBreakEven[idx] = true;
        }
      if(idx < 0)
         continue;

      const double risk = g_trkRisk[idx];
      double newSl = curSl;

      // --- Break-even ---
      if(InpBreakEvenAtR > 0.0 && !g_trkBreakEven[idx] && risk > 0.0)
        {
         const double profit = isLong ? (market - entry) : (entry - market);
         if(profit >= InpBreakEvenAtR * risk)
           {
            const double offset = InpBreakEvenOffsetPt * point;
            const double target = isLong ? (entry + offset) : (entry - offset);
            if(curSl <= 0.0 || (isLong && target > curSl) || (!isLong && target < curSl))
               newSl = target;
            g_trkBreakEven[idx] = true;
           }
        }

      // --- Trailing ---
      if(InpTrailMode == TRAIL_ATR && g_lastAtr > 0.0)
        {
         const double distance = InpTrailAtrMult * g_lastAtr;
         const double target   = isLong ? (market - distance) : (market + distance);
         if(curSl <= 0.0 || (isLong && target > newSl) || (!isLong && target < newSl))
            newSl = target;
        }
      else
         if(InpTrailMode == TRAIL_SMART && g_lastSmartTrail > 0.0)
           {
            const double target = g_lastSmartTrail;
            const bool   valid  = isLong ? (target < market) : (target > market);
            if(valid && (curSl <= 0.0 || (isLong && target > newSl) || (!isLong && target < newSl)))
               newSl = target;
           }

      if(newSl == curSl)
         continue;

      // Never trail into an unprofitable stop or inside the broker's stop level.
      if(isLong)
        {
         if(newSl < entry && InpTrailMode != TRAIL_OFF && g_trkBreakEven[idx])
            continue;
         newSl = MathMin(newSl, market - minDist);
        }
      else
        {
         if(newSl > entry && InpTrailMode != TRAIL_OFF && g_trkBreakEven[idx])
            continue;
         newSl = MathMax(newSl, market + minDist);
        }

      newSl = NormalizeDouble(newSl, digits);
      if(newSl <= 0.0 || MathAbs(newSl - curSl) < point)
         continue;
      if(curSl > 0.0 && ((isLong && newSl <= curSl) || (!isLong && newSl >= curSl)))
         continue;

      if(!g_trade.PositionModify(ticket, newSl, curTp))
         PrintFormat("Stop update failed for #%I64u: retcode %u (%s)",
                     ticket, g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription());
     }
  }

//+------------------------------------------------------------------+
//| Diagnostics                                                      |
//+------------------------------------------------------------------+
void PrintDebugWindow(const double &st[], const double &smaValues[], const int lastIndex)
  {
   if(InpDebugBars <= 0)
      return;

   const int from = (int)MathMax(0, lastIndex - InpDebugBars + 1);
   const int digits = g_symbol.Digits();
   Print("--- indicator window (oldest to newest) ---");
   for(int i = from; i <= lastIndex; i++)
     {
      PrintFormat("%s close=%s st=%s sma=%s",
                  TimeToString(g_rates[i].time, TIME_DATE | TIME_MINUTES),
                  DoubleToString(g_close[i], digits),
                  IsNA(st[i]) ? "na" : DoubleToString(st[i], digits),
                  IsNA(smaValues[i]) ? "na" : DoubleToString(smaValues[i], digits));
     }
   PrintFormat("overlays: %s=%d %s=%d %s=%d %s=%d %s=%d %s=%d %s=%d",
               g_overlayName[0], g_overlayDir[0],
               g_overlayName[1], g_overlayDir[1],
               g_overlayName[2], g_overlayDir[2],
               g_overlayName[3], g_overlayDir[3],
               g_overlayName[4], g_overlayDir[4],
               g_overlayName[5], g_overlayDir[5],
               g_overlayName[6], g_overlayDir[6]);
  }

//+------------------------------------------------------------------+
//| Evaluate the just-closed bar and act on it.                      |
//+------------------------------------------------------------------+
void EvaluateClosedBar()
  {
   if(!LoadRates())
      return;

   if(g_n < InpMinWarmupBars)
     {
      if(!g_warmupLogged)
        {
         PrintFormat("Warming up: %d of %d bars available; no trading yet.", g_n, InpMinWarmupBars);
         g_warmupLogged = true;
        }
      return;
     }

   const int i = g_n - 1;

   double st[];
   int    stDir[];
   SeriesSupertrend(InpSensitivity, InpAtrLen, st, stDir);

   double smaValues[];
   SeriesSma(g_close, g_n, InpSmaLen, smaValues);

   if(IsNA(st[i]) || IsNA(st[i - 1]) || IsNA(smaValues[i]))
      return;

   g_lastSupertrend = st[i];

   double atrValues[];
   SeriesAtr((int)MathMax(InpTrailAtrPeriod, 1), atrValues);
   g_lastAtr = IsNA(atrValues[i]) ? 0.0 : atrValues[i];

   const double closeI = g_close[i];
   const bool isBull = SeriesCrossOver(g_close, st, i) && closeI >= smaValues[i];
   const bool isBear = SeriesCrossUnder(g_close, st, i) && closeI <= smaValues[i];

   // Overlays are needed for the gate, for diagnostics and for the Smart Trail
   // trailing source, so they are refreshed on every closed bar.
   ComputeOverlays(i);

   if(InpDebugBars > 0)
      PrintDebugWindow(st, smaValues, i);

   if(!isBull && !isBear)
     {
      if(InpVerboseLog)
         PrintFormat("%s no signal (close %s, supertrend %s)",
                     TimeToString(g_rates[i].time, TIME_DATE | TIME_MINUTES),
                     DoubleToString(closeI, g_symbol.Digits()),
                     DoubleToString(st[i], g_symbol.Digits()));
      return;
     }

   const int want = isBull ? 1 : -1;

   int confirmations = 0;
   int enabledCount  = 0;
   if(!PassesConfluence(want, confirmations, enabledCount))
     {
      if(InpVerboseLog)
         PrintFormat("%s signal blocked by confluence (%d of %d overlays agree)",
                     isBull ? "BUY" : "SELL", confirmations, enabledCount);
      return;
     }

   string vetoReason = "";
   if(IsVetoed(i, want, vetoReason))
     {
      if(InpVerboseLog)
         PrintFormat("%s signal vetoed: %s", isBull ? "BUY" : "SELL", vetoReason);
      return;
     }

   OpenTrade(isBull, closeI, st[i], confirmations, enabledCount);
  }

//+------------------------------------------------------------------+
//| Apply the guards, size the trade and send it.                    |
//+------------------------------------------------------------------+
void OpenTrade(const bool isBuy, const double signalClose, const double supertrendValue,
               const int confirmations, const int enabledCount)
  {
   string reason = "";

   if(!TradingAllowedNow(reason) || !SessionAllowsTrading(reason) || !SpreadAcceptable(reason))
     {
      if(InpVerboseLog)
         PrintFormat("Entry skipped: %s.", reason);
      return;
     }

   RefreshDayStamp();
   if(DailyLossLimitHit(reason))
     {
      PrintFormat("Entry skipped: %s.", reason);
      return;
     }

   if(InpMinBarsBetween > 0 && g_lastEntryBar > 0)
     {
      const int barsSince = Bars(_Symbol, g_tf, g_lastEntryBar, g_rates[g_n - 1].time);
      if(barsSince < InpMinBarsBetween)
        {
         if(InpVerboseLog)
            PrintFormat("Entry skipped: only %d bars since the last entry.", barsSince);
         return;
        }
     }

   const int want = isBuy ? 1 : -1;

   if(InpCloseOnOpposite && CountPositions(-want) > 0)
      CloseDirection(-want);

   if(InpOnePerDirection && CountPositions(want) > 0)
     {
      if(InpVerboseLog)
         Print("Entry skipped: a position already exists in this direction.");
      return;
     }
   if(InpMaxPositions > 0 && CountPositions(0) >= InpMaxPositions)
     {
      if(InpVerboseLog)
         Print("Entry skipped: maximum concurrent positions reached.");
      return;
     }

   if(!g_symbol.RefreshRates())
      return;

   const double price   = isBuy ? g_symbol.Ask() : g_symbol.Bid();
   const double minDist = MinStopDistance();
   const double perUnit = MoneyPerPriceUnitPerLot();

   double stopDistance = 0.0;
   double takeDistance = 0.0;
   double volume       = 0.0;

   if(InpUseHardTargets)
     {
      // Fixed money targets require a known lot size, so they run on fixed lots.
      volume = NormalizeVolume(InpFixedLot);
      if(perUnit <= 0.0 || volume <= 0.0)
        {
         Print("Entry skipped: cannot convert money targets into price distance for this symbol.");
         return;
        }
      stopDistance = InpHardSlMoney / (volume * perUnit);
      takeDistance = InpHardTpMoney / (volume * perUnit);
     }
   else
     {
      // The stop is the Supertrend line, exactly as the source study uses it.
      stopDistance = MathAbs(signalClose - supertrendValue);
      if(stopDistance <= 0.0)
        {
         Print("Entry skipped: the Supertrend stop distance is zero.");
         return;
        }
      takeDistance = InpRiskReward * stopDistance;
     }

   stopDistance = MathMax(stopDistance, minDist);
   takeDistance = MathMax(takeDistance, minDist);

   if(!InpUseHardTargets)
     {
      if(InpSizingMode == SIZING_FIXED_LOT)
         volume = NormalizeVolume(InpFixedLot);
      else
        {
         if(perUnit <= 0.0)
           {
            Print("Entry skipped: the symbol does not expose a usable tick value.");
            return;
           }
         const double riskMoney  = AccountInfoDouble(ACCOUNT_EQUITY) * InpRiskPercent / 100.0;
         const double lossPerLot = stopDistance * perUnit;
         if(lossPerLot <= 0.0)
           {
            Print("Entry skipped: cannot derive a loss-per-lot for this symbol.");
            return;
           }
         volume = NormalizeVolume(riskMoney / lossPerLot);
        }
     }

   volume = FitVolumeToMargin(isBuy, volume, price);
   if(volume <= 0.0)
     {
      Print("Entry skipped: not enough free margin for the minimum lot.");
      return;
     }

   double sl = 0.0;
   double tp = 0.0;
   if(InpUseStopLoss)
      sl = isBuy ? (price - stopDistance) : (price + stopDistance);
   if(InpUseTakeProfit)
      tp = isBuy ? (price + takeDistance) : (price - takeDistance);

   PrintFormat("Signal %s on %s: confluence %d/%d, stop distance %s.",
               isBuy ? "BUY" : "SELL", _Symbol, confirmations, enabledCount,
               DoubleToString(stopDistance, g_symbol.Digits()));

   if(SendMarketOrder(isBuy, volume, sl, tp, stopDistance))
      g_lastEntryBar = g_rates[g_n - 1].time;
  }

//+------------------------------------------------------------------+
//| Expert initialization                                            |
//+------------------------------------------------------------------+
int OnInit()
  {
   if(InpSensitivity <= 0.0)
     {
      Print("Sensitivity must be greater than zero.");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpAtrLen < 1 || InpSmaLen < 1)
     {
      Print("ATR and SMA lengths must be at least 1.");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpHistoryBars < 100)
     {
      Print("History bars must be at least 100 for the recursive series to settle.");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpMinWarmupBars < 50 || InpMinWarmupBars > InpHistoryBars)
     {
      Print("Warmup bars must be between 50 and the history-bar count.");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpConfluenceThreshold < 0 || InpConfluenceThreshold > OV_COUNT)
     {
      Print("Confluence threshold must be between 0 and 7.");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpRiskReward <= 0.0)
     {
      Print("Risk-reward must be greater than zero.");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpFixedLot <= 0.0)
     {
      Print("Fixed lot must be greater than zero.");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpSizingMode == SIZING_PERCENT_EQUITY && (InpRiskPercent <= 0.0 || InpRiskPercent > 100.0))
     {
      Print("Risk percent must be greater than 0 and no more than 100.");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpUseHardTargets && (InpHardSlMoney <= 0.0 || InpHardTpMoney <= 0.0))
     {
      Print("Fixed money targets must be greater than zero.");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpUseHardTargets && InpSizingMode == SIZING_PERCENT_EQUITY)
     {
      Print("Fixed money targets need a known lot size: select fixed-lot sizing.");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpSessionStartHour < 0 || InpSessionStartHour > 23 ||
      InpSessionEndHour < 0 || InpSessionEndHour > 23)
     {
      Print("Session hours must be between 0 and 23.");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpUseSessionFilter && InpSessionStartHour == InpSessionEndHour)
     {
      Print("The session start and end hour must differ.");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpMaxPositions < 0 || InpOrderRetries < 0 || InpSlippagePoints < 0)
     {
      Print("Position, retry and slippage limits cannot be negative.");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpMaxDailyLossPct < 0.0 || InpMaxDailyLossPct > 100.0)
     {
      Print("The daily loss limit must be between 0 and 100 percent.");
      return(INIT_PARAMETERS_INCORRECT);
     }

   g_tf = (InpTimeframe == PERIOD_CURRENT) ? Period() : InpTimeframe;

   if(!g_symbol.Name(_Symbol))
     {
      Print("Cannot select the chart symbol.");
      return(INIT_FAILED);
     }
   g_symbol.Refresh();
   g_symbol.RefreshRates();

   g_trade.SetExpertMagicNumber((ulong)InpMagicNumber);
   g_trade.SetDeviationInPoints((ulong)InpSlippagePoints);
   g_trade.SetTypeFillingBySymbol(_Symbol);
   g_trade.SetAsyncMode(false);
   g_trade.LogLevel(InpVerboseLog ? LOG_LEVEL_ALL : LOG_LEVEL_ERRORS);

   g_overlayOn[OV_RANGE_FILTER] = InpUseRangeFilter;
   g_overlayOn[OV_SUPERICHI]    = InpUseSuperIchi;
   g_overlayOn[OV_TBO]          = InpUseTbo;
   g_overlayOn[OV_SMART_TRAIL]  = InpUseSmartTrail;
   g_overlayOn[OV_HA_BIAS]      = InpUseHaBias;
   g_overlayOn[OV_MACD]         = InpUseMacdColor;
   g_overlayOn[OV_PSAR]         = InpUsePsar;

   g_overlayName[OV_RANGE_FILTER] = "rangeFilter";
   g_overlayName[OV_SUPERICHI]    = "superIchi";
   g_overlayName[OV_TBO]          = "tbo";
   g_overlayName[OV_SMART_TRAIL]  = "smartTrail";
   g_overlayName[OV_HA_BIAS]      = "haBias";
   g_overlayName[OV_MACD]         = "macd";
   g_overlayName[OV_PSAR]         = "psar";

   ArrayInitialize(g_overlayDir, 0);

   g_lastBarTime  = 0;
   g_firstBarSeen = false;
   g_warmupLogged = false;
   g_lastEntryBar = 0;
   g_dayStamp     = -1;
   RefreshDayStamp();

   ArrayResize(g_trkTicket, 0);
   ArrayResize(g_trkRisk, 0);
   ArrayResize(g_trkBreakEven, 0);

   PrintFormat("Initialised on %s %s. Confluence mode %d, overlays enabled: %d.",
               _Symbol, EnumToString(g_tf), (int)InpConfluenceMode, CountEnabledOverlays());

   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
//| Number of enabled overlays.                                      |
//+------------------------------------------------------------------+
int CountEnabledOverlays()
  {
   int count = 0;
   for(int i = 0; i < OV_COUNT; i++)
      if(g_overlayOn[i])
         count++;
   return(count);
  }

//+------------------------------------------------------------------+
//| Expert deinitialization                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   ArrayFree(g_rates);
   ArrayFree(g_open);
   ArrayFree(g_high);
   ArrayFree(g_low);
   ArrayFree(g_close);
   ArrayFree(g_trkTicket);
   ArrayFree(g_trkRisk);
   ArrayFree(g_trkBreakEven);
   PrintFormat("Stopped (reason %d).", reason);
  }

//+------------------------------------------------------------------+
//| Expert tick                                                      |
//+------------------------------------------------------------------+
void OnTick()
  {
   TrackPrune();
   ManageOpenPositions();

   const datetime barTime = iTime(_Symbol, g_tf, 0);
   if(barTime == 0 || barTime == g_lastBarTime)
      return;

   g_lastBarTime = barTime;

   // The first detection only establishes the baseline: the bar that closed
   // before the expert was attached is not a fresh signal.
   if(!g_firstBarSeen)
     {
      g_firstBarSeen = true;
      return;
     }

   RefreshDayStamp();
   EvaluateClosedBar();
  }
//+------------------------------------------------------------------+
