# Vendored UNCHANGED from Helix's owner (research.recv3), received 2026-10-07 as
# ~/kpc/pylib/research/recv3.py (mtime 2026-08-31, md5 1e46362d564f26dda655bb4650700de3).
# Only these header lines were added. It has no hardcoded tape path: every function
# takes a file path, and the directory comes from HELIX_TAPE_DIR in reader.py.
# Import research.helix.reader, never this module directly (see README.md).
# ruff: noqa
# research/src/research/recv3.py
"""V3 decoder for Helix recorder .hlxz files — STREAMING.

VERIFIED against yesterday's VPS data 2026-06-04:
  SpotQuote = TypeTag 15, 64B; TradePrint = TypeTag 3, 72B.

MEMORY SAFETY: a full day decompresses to several GB. We NEVER hold the whole
decompressed file or a full-day list. iter_quotes/iter_trades are generators
that decompress ONE zstd chunk at a time and yield records. The list-returning
decode_quotes/decode_trades are bounded by `limit`.

UNITS (P1.0 ns time-plane migration, Task 3, 2026-07-05/06): All iterators
yield NANOSECONDS since this migration; legacy microsecond recordings are
normalized *1000 on read. Every time-field-carrying dataclass was renamed
*_us -> *_ns to reflect this (ts_received_ns, ts_wall_ns, ts_publisher_ns,
ts_recorded_ns). This module DUAL-DECODES two TypeTag generations per stream:
the legacy tag (e.g. SpotQuote=15) recorded before the writer flip, whose
on-disk int64/uint64 time fields are genuine MICROSECONDS (empirically
verified against a real VPS recording, Lighter_BBO 20260606.hlxz, 2026-07-06
— decoding those bytes as ns gives a bogus 1970 date, as us gives the correct
2026-06-06 date), and the new canonical *Ns tag (e.g. SpotQuoteNs=22) whose
time fields are already ns at write time. `_DUAL_TAG` maps each canonical tag
number to (legacy tag number, scale) and every typed iterator accepts BOTH
tags for its stream, scaling only the legacy-tag records' time fields by
`scale` (1000) before yielding — callers see one consistent ns-scale field
regardless of which on-disk generation a given record is. Non-time fields
(price, size, asset_id, ...) are identical between the two tag generations
(same payload struct, only the on-disk TAG integer and time-field unit
differ), so no other field needs conversion.
"""

import io
import itertools
import struct
from dataclasses import dataclass
from typing import Iterator, List, Optional

import zstandard as zstd

# ---------------------------------------------------------------------------
# Asset-id → human-readable name.  Mirrors helix/ui ASSET_NAMES / asset.hpp.
# Prediction-window slots 0..13 are optional; spot slots 14..34 are the core
# set; alt-universe perp slots 35..138 are the Lighter alt-universe families.
# ---------------------------------------------------------------------------
ASSET_NAMES: dict[int, str] = {
    # Prediction-window slots (optional — included for completeness)
    # REGENERATED from asset.hpp's HELIX_ASSETID_SLOTS (2026-08-16, P4) —
    # the map had been stale at 139 entries through every append wave.
    # Ids >= 255 (P4 unfreeze): W-generation records carry them in
    # asset_wide (u16); the u8 asset byte holds the 0xFF sentinel.
    0: "Btc5m",
    1: "Btc15m",
    2: "Eth5m",
    3: "Eth15m",
    4: "Sol5m",
    5: "Sol15m",
    6: "Xrp5m",
    7: "Xrp15m",
    8: "Bnb5m",
    9: "Bnb15m",
    10: "Doge5m",
    11: "Doge15m",
    12: "Hype5m",
    13: "Hype15m",
    14: "UsdcSpot",
    15: "BtcSpot",
    16: "EthSpot",
    17: "SolSpot",
    18: "XrpSpot",
    19: "BnbSpot",
    20: "DogeSpot",
    21: "HypeSpot",
    22: "UsdsSpot",
    23: "Usd1Spot",
    24: "PaxSpot",
    25: "UsdtSpot",
    26: "XauSpot",
    27: "SilverSpot",
    28: "Xyz100Spot",
    29: "Sp500Spot",
    30: "BrentOilSpot",
    31: "SpcxSpot",
    32: "SkhxSpot",
    33: "MuSpot",
    34: "WtiOilSpot",
    35: "LitSpot",
    36: "ZecSpot",
    37: "XplSpot",
    38: "NearSpot",
    39: "AsterSpot",
    40: "XmrSpot",
    41: "GramSpot",
    42: "WldSpot",
    43: "UniSpot",
    44: "StrcSpot",
    45: "EnaSpot",
    46: "BotSpot",
    47: "VvvSpot",
    48: "DramSpot",
    49: "TaoSpot",
    50: "XlmSpot",
    51: "AdaSpot",
    52: "AaveSpot",
    53: "PumpSpot",
    54: "AeroSpot",
    55: "FartcoinSpot",
    56: "SuiSpot",
    57: "Pepe1000Spot",
    58: "AvaxSpot",
    59: "TrumpSpot",
    60: "TiaSpot",
    61: "BchSpot",
    62: "TrxSpot",
    63: "OndoSpot",
    64: "UselessSpot",
    65: "MonSpot",
    66: "LinkSpot",
    67: "PenguSpot",
    68: "PendleSpot",
    69: "ZroSpot",
    70: "OpSpot",
    71: "LdoSpot",
    72: "EthfiSpot",
    73: "LiteSpot",
    74: "WlfiSpot",
    75: "SeiSpot",
    76: "BioSpot",
    77: "HbarSpot",
    78: "JupSpot",
    79: "CrvSpot",
    80: "WifSpot",
    81: "ArbSpot",
    82: "JtoSpot",
    83: "Shib1000Spot",
    84: "AptSpot",
    85: "DotSpot",
    86: "FilSpot",
    87: "PythSpot",
    88: "PolSpot",
    89: "CbrsSpot",
    90: "MntSpot",
    91: "IcpSpot",
    92: "LtcSpot",
    93: "MegaSpot",
    94: "MorphoSpot",
    95: "IpSpot",
    96: "Bonk1000Spot",
    97: "DydxSpot",
    98: "GmxSpot",
    99: "EdgeSpot",
    100: "VirtualSpot",
    101: "LineaSpot",
    102: "EigenSpot",
    103: "PopcatSpot",
    104: "SyrupSpot",
    105: "GrassSpot",
    106: "StableSpot",
    107: "NmrSpot",
    108: "StrkSpot",
    109: "SkySpot",
    110: "MetSpot",
    111: "ZkSpot",
    112: "BeraSpot",
    113: "DashSpot",
    114: "MyxSpot",
    115: "ZeroGSpot",
    116: "AztecSpot",
    117: "RailSpot",
    118: "RiverSpot",
    119: "FogoSpot",
    120: "KaitoSpot",
    121: "ProveSpot",
    122: "SSpot",
    123: "DoloSpot",
    124: "EdenSpot",
    125: "SkrSpot",
    126: "AvntSpot",
    127: "StblSpot",
    128: "AxsSpot",
    129: "CroSpot",
    130: "CtrSpot",
    131: "ZoraSpot",
    132: "Floki1000Spot",
    133: "ResolvSpot",
    134: "ArcSpot",
    135: "TwoZSpot",
    136: "ApexSpot",
    137: "FfSpot",
    138: "Not1000Spot",
    139: "BlurSpot",
    140: "ChipSpot",
    141: "SpxSpot",
    142: "PaxgSpot",
    143: "HmstrSpot",
    144: "VineSpot",
    145: "PolyxSpot",
    146: "BananaSpot",
    147: "InjSpot",
    148: "FetSpot",
    149: "CcSpot",
    150: "AlgoSpot",
    151: "AtomSpot",
    152: "TrbSpot",
    153: "MinaSpot",
    154: "WSpot",
    155: "LayerSpot",
    156: "HemiSpot",
    157: "ZenSpot",
    158: "AltSpot",
    159: "InitSpot",
    160: "CeloSpot",
    161: "PurrSpot",
    162: "KasSpot",
    163: "ApeSpot",
    164: "IoSpot",
    165: "OrdiSpot",
    166: "ZetaSpot",
    167: "ImxSpot",
    168: "CakeSpot",
    169: "Neiro1000Spot",
    170: "EtcSpot",
    171: "MantaSpot",
    172: "BsvSpot",
    173: "MoveSpot",
    174: "NilSpot",
    175: "PnutSpot",
    176: "RenderSpot",
    177: "RuneSpot",
    178: "MelaniaSpot",
    179: "GoatSpot",
    180: "SnxSpot",
    181: "BabySpot",
    182: "GriffainSpot",
    183: "HyperSpot",
    184: "TurboSpot",
    185: "MerlSpot",
    186: "IotaSpot",
    187: "CompSpot",
    188: "SushiSpot",
    189: "Lunc1000Spot",
    190: "MemeSpot",
    191: "StxSpot",
    192: "EnsSpot",
    193: "ArSpot",
    194: "MeSpot",
    195: "MoodengSpot",
    196: "TnsrSpot",
    197: "WctSpot",
    198: "AnimeSpot",
    199: "GalaSpot",
    200: "AixbtSpot",
    201: "CfxSpot",
    202: "YggSpot",
    203: "BrettSpot",
    204: "BigtimeSpot",
    205: "GmtSpot",
    206: "NxpcSpot",
    207: "RezSpot",
    208: "PeopleSpot",
    209: "DymSpot",
    210: "SuperSpot",
    211: "SagaSpot",
    212: "SandSpot",
    213: "NeoSpot",
    214: "AceSpot",
    215: "GasSpot",
    216: "UsualSpot",
    217: "SophSpot",
    218: "UmaSpot",
    219: "XaiSpot",
    220: "RsrSpot",
    221: "BomeSpot",
    222: "ChzSpot",
    223: "EgldSpot",
    224: "JasmySpot",
    225: "TonSpot",
    226: "XtzSpot",
    227: "XautSpot",
    228: "Au200Spot",
    229: "Eu50Spot",
    230: "Fr40Spot",
    231: "De40Spot",
    232: "Hk50Spot",
    233: "Jp225Spot",
    234: "Es35Spot",
    235: "Uk100Spot",
    236: "EurSpot",
    237: "GbpSpot",
    238: "BvivSpot",
    239: "EvivSpot",
    240: "XpdSpot",
    241: "XptSpot",
    242: "BeatSpot",
    243: "AprSpot",
    244: "TutSpot",
    245: "CysSpot",
    246: "PromSpot",
    247: "GiggleSpot",
    248: "CapSpot",
    249: "BtwSpot",
    250: "OneSpot",
    251: "VelvetSpot",
    252: "CotiSpot",
    253: "FheSpot",
    254: "HumSpot",
    255: "AkeSpot",
    256: "AioSpot",
    257: "BasedSpot",
    258: "AeonSpot",
    259: "CowSpot",
    260: "BicoSpot",
    261: "TradoorSpot",
    262: "PortalSpot",
    263: "ZbtSpot",
    264: "PrlSpot",
    265: "OpgSpot",
    266: "AlloSpot",
    267: "ZerebroSpot",
    268: "XanSpot",
    269: "PixelSpot",
    270: "PowerSpot",
    271: "PtbSpot",
    272: "ReSpot",
    273: "ReadySpot",
    274: "SlxSpot",
    275: "X2u2Spot",
    276: "LabSpot",
    277: "SkyaiSpot",
    278: "SoonSpot",
    279: "BdxSpot",
    280: "CloSpot",
    281: "BasSpot",
    282: "PippinSpot",
    283: "ShazSpot",
    284: "IcntSpot",
    285: "GigadevSpot",
    286: "BspSpot",
    287: "RaveSpot",
    288: "LightSpot",
    289: "AigensynSpot",
    290: "BsbSpot",
    291: "KgenSpot",
    292: "OhSpot",
    293: "IrysSpot",
    294: "FolksSpot",
    295: "BillSpot",
    296: "GeniusSpot",
    297: "NexSpot",
    298: "HanaSpot",
    299: "HomeSpot",
    300: "EmSpot",
}


def asset_name(aid: int) -> str:
    """Return human-readable name for an asset_id, e.g. 15 -> 'BtcSpot'.

    Falls back to 'asset#<id>' for unknown ids so callers never crash.
    """
    return ASSET_NAMES.get(aid, f"asset#{aid}")


QUOTE_V3 = struct.Struct("<dddd qq Q BBBB 4x")  # 64B
TRADE = struct.Struct("<16s c 7x dd Q qq B B 6x")  # 72B (V3 2026-06-05)
assert QUOTE_V3.size == 64
assert TRADE.size == 72
# TradePrint V3 (2026-06-05): ts_publisher_ns DROPPED (mirrors SpotQuote V3),
# ts_wall_ns (CLOCK_REALTIME anchor) + asset_id (frozen AssetId index) ADDED.
# Layout: symbol[16], side, 7x, price, size, trade_id, ts_received_ns,
#         ts_wall_ns, source, asset_id, 6x. Old tag-3 recordings are unreadable.

POLY_BBO = struct.Struct(
    "<QQQ qqqq 80s 80s B 7x"
)  # 224B PolyClobBbo (tag14): ts_rec,ts_wall,ts_pub,...
POLY_TRADE = struct.Struct("<80s 80s c 7x d d q q Q")  # 208B PolyClobTrade (tag 12)
assert POLY_BBO.size == 224
assert POLY_TRADE.size == 208

# Legacy 40B SpotQuote (TypeTag::SpotQuote=1) — pre-ABI-v2, no sizes/asset_id.
# helix/engine/recorder/legacy_payloads.hpp HelixSpotQuoteV1:
#   double bid; double ask; int64 ts_publisher_us; int64 ts_received_us;
#   uint8 source; uint8 _pad[7];
QUOTE_V1 = struct.Struct("<dd qq B 7x")  # 40B
assert QUOTE_V1.size == 40

# HL perp funding (TypeTag::HlFunding=16, 56B) — helix type_tag.hpp HlFundingPayload:
#   uint64 ts_wall_ns; uint8 asset_id; uint8 _pad[7]; double funding_rate;
#   double premium; double mark_px; double oracle_px; double open_interest;
HL_FUNDING = struct.Struct("<Q B 7x ddddd")  # 56B
assert HL_FUNDING.size == 56

# Stablecoin ratios (TypeTag::StablecoinRatios=10, 32B) — derived from Coinbase
# USDT/USDC-USD top-of-book. As of 2026-06-07 recorded in its OWN source
# (StablecoinRatios/, source id 0x0303); previously co-mingled into
# CoinbaseSpot_BBO. helix data_plane/stablecoin_ratios.hpp:
#   double usd_per_usdt; double usd_per_usdc; int64 ts_received_ns; u8 _pad[8].
STABLECOIN = struct.Struct("<dd q 8x")  # 32B
assert STABLECOIN.size == 32

# Multi-venue funding (TypeTag::MultiVenueFunding=17, 24B) — helix type_tag.hpp
# MultiVenueFundingPayload:
#   uint64 ts_wall_ns; uint8 asset_id; uint8 venue; uint8 _pad[6];
#   double funding_rate;
# venue: 0=lighter, 1=hyperliquid, 2=binance, 3=bybit.
# Lighter's funding-rates endpoint aggregates the HL/Lighter carry across 4
# venues; one record per (family, exchange) per ~1/min poll.
MV_FUNDING = struct.Struct("<Q B B 6x d")  # 24B
assert MV_FUNDING.size == 24

# Generic per-venue perp funding (TypeTag::PerpFunding=19, 56B) — helix
# type_tag.hpp PerpFundingPayload: ts_wall_ns, asset_id, venue, _pad[6],
# funding_rate, premium, mark_px, oracle_px, open_interest. Used by AsterPerp
# (venue=30) + Bulk (venue=33). Same wire layout as HlFunding but with a venue
# byte where HlFunding has its second pad byte.
PERP_FUNDING = struct.Struct("<Q B B 6x ddddd")  # 56B
assert PERP_FUNDING.size == 56

# Kalshi window (TypeTag::KalshiWindow=18, 80B) — helix kalshi_window_payload.hpp
# KalshiWindowPayload:
#   uint64 ts_recorded_us; uint64 open_unix; char market_ticker[48];
#   double strike; uint8 asset_id; uint8 _pad[7];
# One record per (asset, market_ticker) emitted by KalshiFeedHost on
# first-sight / 15-min rotation. Splits a single *15m asset_id BBO/trade
# stream back into individual strike contracts (full ticker + strike +
# open_unix). strike is NaN if the ticker strike was unparsable; open_unix
# is 0 if the ticker date was unparsable.
KALSHI_WINDOW = struct.Struct("<QQ 48s d B 7x")  # 80B
assert KALSHI_WINDOW.size == 80
DAILY_BAR = struct.Struct("<Q B x H 4x dddddd")  # 64B DailyBar (tag 20, src 0x1B00)
# NB: field 2 is the legacy u8 asset_id, field 3 the V4 u16 asset_wide (offset 10,
# claimed from pad 2026-08-14). For a true id >= 256 the u8 holds the 0xFF SENTINEL
# and asset_wide holds the real slot. Always read `asset_wide or asset_id`.
assert DAILY_BAR.size == 64
HOURLY_BAR = struct.Struct(
    "<Q B x H 4x dddddd"
)  # 64B HourlyBar (tag 21, src 0x1B01/0x1B03) — see DAILY_BAR note
assert HOURLY_BAR.size == 64

# Legacy (pre-migration, microsecond-payload) tags. READ-ONLY forever — the
# _LEGACY_US suffix documents the unit; kept unsuffixed for source compat
# (every existing caller/tester references these names).
TAG_QUOTE_V3 = 15  # SpotQuote legacy (us) -- see TAG_QUOTE_NS=22
TAG_QUOTE_V1 = 1
TAG_TRADE = 3  # TradePrint legacy (us) -- see TAG_TRADE_NS=24
TAG_POLY_BBO = 14  # PolyClobBbo legacy (us) -- see TAG_POLY_BBO_NS=27
TAG_POLY_TRADE = 12  # PolyClobTrade legacy (us) -- see TAG_POLY_TRADE_NS=25
TAG_HL_FUNDING = 16  # HlFunding legacy (us) -- see TAG_HL_FUNDING_NS=28
TAG_MV_FUNDING = 17  # MultiVenueFunding legacy (us) -- see TAG_MV_FUNDING_NS=29
TAG_PERP_FUNDING = 19  # PerpFunding legacy (us) -- see TAG_PERP_FUNDING_NS=30
TAG_KALSHI_WINDOW = 18  # KalshiWindow legacy (us) -- see TAG_KALSHI_WINDOW_NS=34
TAG_STABLECOIN = 10  # StablecoinRatios legacy (us, own source 0x0303 since 2026-06-07) -- see TAG_STABLECOIN_NS=31
TAG_LIGHTER_BOOK = (
    2  # Lighter_Book L2 channel legacy (us) (source id 0x1501), 688B/record -- see TAG_BOOK_NS=23
)
TAG_DAILY_BAR = 20  # DailyBar daily OHLCV (source 0x1B00, 64B) — calendar-seconds key, UNAFFECTED by the ns migration
TAG_HOURLY_BAR = 21  # HourlyBar hourly OHLCV (source 0x1B01/0x1B03, 64B) — identical to DailyBar with hour_unix instead of day_unix
# NOTE: TAG_POLY_WINDOW=7 (the historical name) is defined further below, next
# to the POLY_WINDOW struct -- referenced here by _DUAL_TAG via a forward
# literal (7) since Python can't forward-reference a not-yet-defined name in a
# module-level dict built at this point in the file.
_E8 = 1e8

# ns time plane (2026-07-05) writer-only canonical tags — same payload structs
# as their legacy siblings above, time fields already ns at write time. See
# helix/engine/recorder/type_tag.hpp for the authoritative enum.
TAG_QUOTE_NS = 22  # replaces TAG_QUOTE_V3=15
TAG_BOOK_NS = 23  # replaces TAG_LIGHTER_BOOK=2
TAG_TRADE_NS = 24  # replaces TAG_TRADE=3
TAG_POLY_TRADE_NS = 25  # replaces TAG_POLY_TRADE=12
TAG_POLY_BOOK_NS = 26  # replaces PolyClobBook legacy=13 (not separately decoded here)
TAG_POLY_BBO_NS = 27  # replaces TAG_POLY_BBO=14
TAG_HL_FUNDING_NS = 28  # replaces TAG_HL_FUNDING=16
TAG_MV_FUNDING_NS = 29  # replaces TAG_MV_FUNDING=17
TAG_PERP_FUNDING_NS = 30  # replaces TAG_PERP_FUNDING=19
TAG_STABLECOIN_NS = 31  # replaces TAG_STABLECOIN=10
TAG_POLY_WINDOW_NS = 32  # replaces TAG_POLY_WINDOW (legacy)=7
TAG_POLY_RES_NS = 33  # replaces PolymarketResolution legacy=8 (not decoded here)
TAG_KALSHI_WINDOW_NS = 34  # replaces TAG_KALSHI_WINDOW=18

# Canonical Ns tag -> (legacy tag, scale). `scale` is what a LEGACY-tagged
# record's time fields must be multiplied by to reach ns (1000 -- legacy
# payloads hold genuine microseconds, verified empirically -- see the module
# docstring). Canonical-tagged records need no scaling (their time fields are
# already ns at write time); this map's job is purely "given a canonical tag,
# what legacy tag/scale pairs with it for dual-tag decode."
_DUAL_TAG = {
    TAG_QUOTE_NS: (TAG_QUOTE_V3, 1000),
    TAG_BOOK_NS: (TAG_LIGHTER_BOOK, 1000),
    TAG_TRADE_NS: (TAG_TRADE, 1000),
    TAG_POLY_TRADE_NS: (TAG_POLY_TRADE, 1000),
    TAG_POLY_BOOK_NS: (13, 1000),
    TAG_POLY_BBO_NS: (TAG_POLY_BBO, 1000),
    TAG_HL_FUNDING_NS: (TAG_HL_FUNDING, 1000),
    TAG_MV_FUNDING_NS: (TAG_MV_FUNDING, 1000),
    TAG_PERP_FUNDING_NS: (TAG_PERP_FUNDING, 1000),
    TAG_STABLECOIN_NS: (TAG_STABLECOIN, 1000),
    TAG_POLY_WINDOW_NS: (7, 1000),  # TAG_POLY_WINDOW, defined below
    TAG_POLY_RES_NS: (8, 1000),
    TAG_KALSHI_WINDOW_NS: (TAG_KALSHI_WINDOW, 1000),
}

# V4 wide generation (2026-08-14) — W twins of the Ns tags (helix
# type_tag.hpp tags 43-50). SAME payload structs and sizes: a u16
# `asset_wide` was claimed from pad (all pre-existing offsets unchanged),
# so every per-record decoder in this module works verbatim on W-tagged
# records; the u8 asset_id byte stays valid while every live asset < 255.
# Time fields are already ns (scale 1). Funding/bar payloads did NOT get W
# twins (same-tag frozen-append; their assets are *Spot >= 14).
# OrderRequestW/OrderEventW (49/50) carry the ABI-v14 RELAYOUT (asset u16
# at offset 2/50, OrderRequest flags byte at 7, OrderEvent liquidity at
# 52) — those two do NOT share the pre-v14 layout; decode accordingly.
TAG_QUOTE_W = 43
TAG_BOOK_W = 44
TAG_TRADE_W = 45
TAG_POLY_WINDOW_W = 46
TAG_POLY_RES_W = 47
TAG_KALSHI_WINDOW_W = 48
TAG_ORDER_REQ_W = 49
TAG_ORDER_EVT_W = 50

_WIDE_TAG = {
    TAG_QUOTE_NS: TAG_QUOTE_W,
    TAG_BOOK_NS: TAG_BOOK_W,
    TAG_TRADE_NS: TAG_TRADE_W,
    TAG_POLY_WINDOW_NS: TAG_POLY_WINDOW_W,
    TAG_POLY_RES_NS: TAG_POLY_RES_W,
    TAG_KALSHI_WINDOW_NS: TAG_KALSHI_WINDOW_W,
}


def _iter_dual_tag(path, canonical_tag, rsz):
    """Yield (raw_record_bytes, scale) for BOTH the canonical tag and its
    legacy sibling, where `scale` is 1 for canonical-tag records (already ns)
    and 1000 for legacy-tag records (us -> ns). Both tag generations share
    the same payload struct/size, so a single `rsz` filter covers both.

    ORDERING NOTE: this scans the canonical-tag blocks, THEN the legacy-tag
    blocks (two separate passes) rather than a single interleaved walk. For
    the overwhelmingly common case -- a file recorded entirely under ONE
    generation -- this is a no-op (the other pass yields nothing) and file
    order is preserved exactly. For the rare "recorder restarted mid-
    migration" mixed file (legal but atypical), output is grouped by
    generation rather than strict file order; every record is still yielded
    exactly once with correct ns values. Callers needing strict chronological
    order across a mixed file should sort by the yielded ts field."""
    legacy_tag, legacy_scale = _DUAL_TAG[canonical_tag]
    wide_tag = _WIDE_TAG.get(canonical_tag)
    if wide_tag is not None:  # V4 W twin: identical struct, already-ns
        for rec in iter_records(path, tag=wide_tag, rsz=rsz):
            yield rec, 1
    for rec in iter_records(path, tag=canonical_tag, rsz=rsz):
        yield rec, 1
    for rec in iter_records(path, tag=legacy_tag, rsz=rsz):
        yield rec, legacy_scale


# v1 .hlxz container structs — MUST match helix/engine/recorder/v1_format.hpp.
# A .hlxz file = [FileHeader][Block]...[Block][FooterIndex?][FooterTrailer?]
# where Block = [BlockHeader][one zstd frame of homogeneous same-tag records].
# The `4x` trailing pad in BLK_HDR/IDX_ENT is consumed (not returned) by unpack,
# so _BLK_HDR.unpack* returns 9 values and _IDX_ENT.unpack* returns 7 values.
_FILE_HDR = struct.Struct("<IHHHHQIQ")  # 32B
_BLK_HDR = struct.Struct("<IBBHIIIqq4x")  # 40B
_IDX_ENT = struct.Struct("<QIIB3sqq4x")  # 40B
_TRAILER = struct.Struct("<QIIQ")  # 24B
_FILE_MAGIC = 0x31584C48
_BLK_MAGIC = 0x42584C48
_FOOT_MAGIC = 0x31544F4F46584C48
_FOOTER_PRESENT = 0x1

# HLXF frame-stream format — the recorder switched from the indexed .hlxz
# CONTAINER (above) to a plain concatenated FRAME stream (2026-06-06 VPS
# upgrade). MUST match helix/engine/recorder/frame_format.hpp:
#   FrameHeader (24B): u32 magic='HLXF' | u16 source_id | u8 type_tag |
#   u8 flags | u32 record_count | u32 record_size | i64 first_ts_ns
# then record_count * record_size raw POD payloads (SAME per-tag structs as
# the container format — only the framing changed). The whole stream is
# zstd-compressed as the legacy .bin.zst file body (pre-2026-06-06 recorder).
# The current recorder writes .hlxz (v1 container format) instead.
_FRAME_HDR = struct.Struct("<IHBBIIq")  # 24B
_FRAME_MAGIC = 0x484C5846  # 'HLXF'


def _zstd_decompress_all(data: bytes) -> bytes:
    """Decompress a zstd-wrapped file body, handling BOTH the v1 single-stream
    form AND the v2 MULTI-FRAME form (many small independent zstd frames
    concatenated — one per recorder flush). `read_across_frames=True` decodes
    multiframe natively and FAST (single C call), but raises if the FINAL frame
    is truncated (file still being written). We read it incrementally so a tail
    error keeps everything decoded so far instead of failing the whole file."""
    reader = zstd.ZstdDecompressor().stream_reader(io.BytesIO(data), read_across_frames=True)
    out = bytearray()
    try:
        while True:
            chunk = reader.read(1 << 24)  # 16 MiB chunks
            if not chunk:
                break
            out += chunk
    except zstd.ZstdError:
        pass  # truncated/garbled final frame — keep the decoded prefix
    if out:
        return bytes(out)
    # Fast path yielded nothing (the FIRST frame can trip read_across_frames on
    # some files). Fall back to walking frames one at a time via unused_data —
    # slower but robust, and still tolerant of a partial final frame.
    dctx = zstd.ZstdDecompressor()
    buf = data
    while buf[:4] == b"\x28\xb5\x2f\xfd":
        dobj = dctx.decompressobj()
        try:
            out += dobj.decompress(buf)
        except zstd.ZstdError:
            break
        nxt = dobj.unused_data
        if not nxt or len(nxt) >= len(buf):
            break
        buf = nxt
    return bytes(out)


def _iter_hlxf_records(data: bytes, tag: Optional[int]) -> Iterator[bytes]:
    """Walk a decompressed HLXF frame stream, yielding raw record bytes.
    `data` is the already-zstd-decompressed file body."""
    off = 0
    n = len(data)
    while off + 24 <= n:
        (magic, _src, ftag, _flags, count, rsz, _fts) = _FRAME_HDR.unpack_from(data, off)
        if magic != _FRAME_MAGIC or rsz == 0:
            break  # not a frame / corruption / partial tail
        body = off + 24
        end = body + count * rsz
        if end > n:
            break  # partial tail
        if tag is None or ftag == tag:
            for i in range(count):
                yield data[body + i * rsz : body + (i + 1) * rsz]
        off = end


@dataclass
class QuoteV3:
    ts_received_ns: int
    ts_wall_ns: int
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    seq: int
    source: int
    quote_ccy: int
    asset_id: int
    changed: int


@dataclass
class TradeV3:
    ts_received_ns: int
    ts_wall_ns: int  # V3 (2026-06-05): CLOCK_REALTIME unix calendar anchor
    side: str  # "B" / "S"
    price: float
    size: float
    trade_id: int
    source: int
    asset_id: int  # V3 (2026-06-05): frozen AssetId index — split per family


@dataclass
class PolyClobBbo:
    ts_received_ns: int  # ts_recorded_ns (engine receive)
    ts_publisher_ns: int
    bid_px: float  # 0..1
    bid_sz: float
    ask_px: float
    ask_sz: float
    outcome: int  # 0=YES, 1=NO
    condition_id: str  # 0x-hex condition id ('' if no current window)
    token_id: str  # this outcome's CLOB token (decimal u256), '' if none
    ts_wall_ns: int = 0  # CLOCK_REALTIME — calendar anchor (Kalshi bridge)


@dataclass
class PolyClobTrade:
    ts_received_ns: int
    ts_publisher_ns: int
    side: str  # 'B'/'S'
    price: float  # 0..1
    size: float
    condition_id: str
    asset_id: str
    seq: int


@dataclass
class HlFunding:
    ts_wall_ns: int  # CLOCK_REALTIME at poll (cross-venue clock)
    asset_id: int  # frozen AssetId index (BtcSpot=15 .. HypeSpot=21)
    funding_rate: float  # 1h funding (fraction; + => HL SHORT earns it)
    premium: float  # premium index (perp vs oracle), fraction
    mark_px: float
    oracle_px: float
    open_interest: float


@dataclass
class MultiVenueFunding:
    ts_wall_ns: int  # CLOCK_REALTIME at poll (cross-venue clock)
    asset_id: int  # frozen AssetId index (BtcSpot=15 .. HypeSpot=21)
    venue: int  # 0=lighter,1=hyperliquid,2=binance,3=bybit,4=aster,5=bulk
    funding_rate: float  # funding fraction (+ => a SHORT earns it)


@dataclass
class DailyBar:
    """Daily OHLCV bar (TypeTag::DailyBar=20, 64B, source 0x1B00 DailyBar_Lighter).
    One record per (asset, day). day_unix is 00:00-UTC epoch SECONDS. Seeded from
    Lighter /api/v1/candles by xsec_backfill, grown daily by daily_bar_collector.
    The cross-sectional momentum strategy ranks on `close`. Live-collected bars may
    carry only `close` (OHLC/volume 0); backfilled bars carry full OHLCV."""

    day_unix: int  # 00:00-UTC epoch seconds (day_unix % 86400 == 0)
    asset_id: int  # frozen *Spot AssetId slot (V4-wide aware; see DAILY_BAR)
    open: float
    high: float
    low: float
    close: float
    volume_base: float
    volume_quote: float


@dataclass
class HourlyBar:
    """Hourly OHLCV bar (TypeTag::HourlyBar=21, 64B, source 0x1B01 HourlyBar_Lighter or 0x1B03 HourlyBar_Hyperliquid).
    One record per (asset, hour). hour_unix is top-of-hour epoch SECONDS (hour_unix % 3600 == 0).
    Seeded from venue /candles APIs by xsec_hourly_backfill, grown hourly by hourly_bar_collector.
    Identical layout to DailyBar with hour_unix instead of day_unix."""

    hour_unix: int  # top-of-hour epoch seconds (hour_unix % 3600 == 0)
    asset_id: int  # frozen *Spot AssetId slot (V4-wide aware; see DAILY_BAR)
    open: float
    high: float
    low: float
    close: float
    volume_base: float
    volume_quote: float


@dataclass
class PerpFunding:
    """Generic per-venue perp funding (TypeTag::PerpFunding=19, 56B). Emitted
    1/min by AsterPerp (venue=30) and Bulk (venue=33) feed hosts from their own
    funding endpoints. Same fields a SHORT earns when funding_rate>0. Fields a
    venue doesn't expose are 0 (Aster: premium+oi=0; Bulk: premium=0)."""

    ts_wall_ns: int  # CLOCK_REALTIME at poll (cross-venue clock)
    asset_id: int  # frozen AssetId index (BtcSpot=15 .. HypeSpot=21)
    venue: int  # guardian::hub::Venue byte (AsterPerp=30, Bulk=33)
    funding_rate: float  # funding fraction (+ => a SHORT earns it)
    premium: float  # premium index (perp vs oracle); 0 if absent
    mark_px: float
    oracle_px: float
    open_interest: float  # base units; 0 if absent


@dataclass
class KalshiWindow:
    ts_recorded_ns: int  # wall-clock at record
    open_unix: int  # window open (seconds since epoch); 0 if unparsable
    market_ticker: str  # full Kalshi ticker, e.g. "KXBTC15M-26MAY061515-15"
    strike: float  # parsed strike; NaN if unparsable
    asset_id: int  # frozen AssetId (the *15m slot)


# Polymarket window (TypeTag=7, 256B) — helix poly_window_payloads.hpp
# PolyWindowPayload: ts_recorded_ns, open_unix, condition_id[64], token_yes[80],
# token_no[80], strike, asset, _pad[7]. Maps a Poly 15m window to its YES/NO
# CLOB token ids (which key the Polymarket_CLOB_{Yes,No}_BBO streams) + strike —
# the cross-venue join key against KalshiWindow's (asset, open_unix, strike).
POLY_WINDOW = struct.Struct("<QQ 64s 80s 80s d B 7x")  # 256B
assert POLY_WINDOW.size == 256
TAG_POLY_WINDOW = 7


@dataclass
class PolyWindow:
    ts_recorded_ns: int
    open_unix: int  # window open (seconds since epoch)
    condition_id: str  # 0x-hex condition id
    token_yes: str  # CLOB token id for YES (decimal u256 string)
    token_no: str  # CLOB token id for NO
    strike: float  # PMPS strike; NaN if unfetched
    asset: int  # helix::Asset enum


@dataclass
class BookL2:
    # Lighter_Book L2 snapshot. Record is 688B = 43 (price,size) f64 pairs:
    #   pairs[0:20]  = bids, DESCENDING from best bid (bids[0]=top bid)
    #   pairs[20:40] = asks, ASCENDING from best ask (asks[0]=top ask)
    #   pairs[40:43] = zero padding (unused level slots)
    # ts/source live in the 24B FRAME_HDR, NOT the payload (HLXF format only —
    # the v1 container's per-record ts is the metadata-trailer field read
    # below). The feed MULTIPLEXES all asset families under one source id
    # (0x1501) with NO per-record asset tag pre-V3, so asset_id falls back to
    # price-magnitude inference (see _book_asset_of) when the trailer's
    # asset_id byte is 0 (pre-V3 recordings).
    ts_received_ns: int
    asset_id: int  # V3 (2026-06-06): frozen AssetId index from the record
    # trailer; pre-V3 records fall back to price-band infer.
    source: int  # frame source id (0x1501 = Lighter book)
    bids: List[tuple]  # [(price, size), ...] descending, best first
    asks: List[tuple]  # [(price, size), ...] ascending, best first


_BOOK_PAIRS = struct.Struct("<86d")  # 43 (price,size) pairs = 86 doubles = 688B
assert _BOOK_PAIRS.size == 688
# The 688B book record IS the C++ guardian::hub::HubOrderbook struct:
#   bids[20] @0..320, asks[20] @320..640  (the 80 doubles _BOOK_PAIRS reads as
#   pairs[0:40]), then the metadata trailer @640..688 — what _BOOK_PAIRS was
#   discarding as "pairs[40:43] padding" is actually:
#     n_bids u8 @640, n_asks u8 @641, _pad0[6] @642, sequence u64 @648,
#     ts_publisher_ns i64 @656, ts_received_ns i64 @664, source u8 @672,
#     asset_id u8 @673 (V3 2026-06-06), _pad1[6] @674.
# Orderbook V3 stamps asset_id at the publish site, so we read it directly
# instead of inferring from the price band. ts_received is us on a legacy-tag
# (tag=2 OrderbookUsLegacy) record, ns on a canonical-tag (tag=23 OrderbookNs)
# record -- iter_book() below applies the *1000 scale per-record based on
# which tag it came from (see _iter_dual_tag / TAG_BOOK_NS).
_BOOK_OFF_ASSET_ID = 673  # offsetof(HubOrderbook, asset_id)
_BOOK_OFF_TS_RECV = 664  # offsetof(HubOrderbook, ts_received_ns)
_BOOK_OFF_SOURCE = 672  # offsetof(HubOrderbook, source)

# Asset inference from top-bid price magnitude on 20260603 (Lighter book has no
# per-record asset tag). Bands are wide and non-overlapping for the 7 families;
# verified against Lighter_BBO ranges. (low, high, asset_id).
_BOOK_PRICE_BANDS = [
    (20000.0, 200000.0, 15),  # BTC ~66000
    (1000.0, 4000.0, 16),  # ETH ~1832
    (300.0, 1000.0, 19),  # BNB ~625
    (40.0, 200.0, 17),  # SOL ~72  (HYPE ~72 too — collides; see note)
    (0.5, 5.0, 18),  # XRP ~1.2
    (0.001, 0.5, 20),  # DOGE ~0.09
]


def _book_asset_of(top_bid: float) -> int:
    """Infer asset_id from a snapshot's best-bid price. SOL and HYPE both sit
    ~70-75 on 20260603 so the 40-200 band returns SOL(17) by convention; callers
    needing HYPE-vs-SOL disambiguation must cross-check the trade/BBO feed. All
    other families are unambiguous."""
    for lo, hi, aid in _BOOK_PRICE_BANDS:
        if lo <= top_bid < hi:
            return aid
    return -1


def iter_records(
    path: str,
    tag: Optional[int] = None,
    t0: Optional[int] = None,
    t1: Optional[int] = None,
    rsz: Optional[int] = None,
) -> Iterator[bytes]:
    """Yield raw record bytes from a v1 .hlxz file. Optional tag/time filters
    skip whole blocks via the footer index without decompressing them.

    When a footer is present we seek to it and walk the index (each entry carries
    the block's file_offset, tag and first/last ts). When it is absent (still
    being written, or footer_present=0) we walk blocks inline from the FileHeader.
    t0/t1 are ns timestamps compared against the block's first_ts_ns/last_ts_ns.

    `rsz`: when given, SKIP any block whose record_size != rsz. A single daily
    file can mix record widths for one tag when a payload schema changed mid-day
    (e.g. PolyClobBbo 56B pre-fix then 216B post-fix appended to the same file).
    A typed reader passes its expected size so stale-width blocks are cleanly
    skipped instead of mis-unpacked.
    """
    # mmap, not read() (2026-08-15): two concurrent iter_records generators on
    # one multi-GB day file (the V4 dual checker zips the Ns and W streams)
    # each held a full private copy of the file — 29.7GB peak RSS on the
    # KuCoinFutures book day. A read-only mmap is a drop-in for every use in
    # this module (unpack_from + slicing) and its pages are reclaimable cache,
    # not anon heap. Empty/tiny files can't be mmapped — fall back to read().
    import mmap as _mmap

    with open(path, "rb") as fh:
        try:
            data = _mmap.mmap(fh.fileno(), 0, access=_mmap.ACCESS_READ)
        except (ValueError, OSError):
            data = fh.read()
    if len(data) < 8:
        return
    # Detect HLXF frame-stream format (2026-06-06 recorder upgrade): the file
    # body is zstd-compressed and begins with the 'HLXF' frame magic once
    # decompressed. The old .hlxz container is NOT outer-zstd-wrapped (its
    # FileHeader magic is at byte 0 in the clear). Branch on the raw zstd magic.
    if data[:4] == b"\x28\xb5\x2f\xfd":  # zstd magic -> HLXF frame stream
        body = _zstd_decompress_all(data)
        if body[:4] == b"FXLH":  # 'HLXF' little-endian
            yield from _iter_hlxf_records(body, tag)
        return
    if len(data) < 32:
        return
    (magic, ver, hsz, _src, _rsv, _cre, flags, _pad) = _FILE_HDR.unpack_from(data, 0)
    if magic != _FILE_MAGIC or ver != 1:
        return  # not a v1 file (clean break: no v0 fallback)
    blocks = []  # (offset, comp_len, count, tag, first_ts, last_ts)
    used_footer = False
    # The footer trailer lives at END-OF-FILE and indexes only the LAST
    # sub-file. helixd re-opens <date>.hlxz in O_APPEND on every restart and
    # writes a fresh FileHeader (and, on clean shutdown, a footer) per
    # restart, so a multi-restart day is several concatenated sub-files.
    # Using the footer would silently read only the last sub-file's blocks
    # and miss everything recorded before the final restart. The footer
    # fast-path is therefore ONLY safe for a SINGLE sub-file. Detect a
    # second embedded FileHeader; if present, always fall through to the
    # inline walk below (which resyncs across every sub-file). Mirrors
    # helix/engine/recorder/block_reader.cpp's BlockReader::open guard.
    multi_subfile = data.find(struct.pack("<I", _FILE_MAGIC), 32) >= 0
    if not multi_subfile and flags & _FOOTER_PRESENT and len(data) >= 32 + 24:
        (fmag, n, ilen, _tot) = _TRAILER.unpack_from(data, len(data) - 24)
        if fmag == _FOOT_MAGIC and ilen == n * 40:
            idx_off = len(data) - 24 - ilen
            if 32 <= idx_off <= len(data):
                for k in range(n):
                    (off, comp, count, btag, _p, fts, lts) = _IDX_ENT.unpack_from(
                        data, idx_off + k * 40
                    )
                    blocks.append((off, comp, count, btag, fts, lts))
                used_footer = True
    if not used_footer:
        off = 32
        n_total = len(data)
        while off + 40 <= n_total:
            magic0 = struct.unpack_from("<I", data, off)[0]
            if magic0 == _FILE_MAGIC:
                # A second .hlxz sub-file concatenated into the same daily file:
                # helixd re-opens <YYYYMMDD>.hlxz in O_APPEND on every restart and
                # writes a fresh FileHeader, so a day with N restarts is
                # [FileHeader][blocks][FooterIndex?][FooterTrailer?]...×N. Skip the
                # 32-byte FileHeader and keep walking the next sub-file's blocks.
                off += 32
                continue
            # NOTE: use walk-local names (bw_*) — do NOT reuse `rsz`/`uncomp`,
            # which would shadow and clobber the function's `rsz` parameter,
            # breaking the per-block rsz filter on mixed-width files.
            (bm, bw_tag, _p0, bw_rsz, bw_count, bw_comp, bw_uncomp, bw_fts, bw_lts) = (
                _BLK_HDR.unpack_from(data, off)
            )
            if bm != _BLK_MAGIC:
                # Not a block or FileHeader: footer index/trailer bytes from a
                # clean-closed sub-file, or corruption. Resync to the next
                # embedded FileHeader if one exists, else stop at the parseable
                # prefix.
                nxt = data.find(struct.pack("<I", _FILE_MAGIC), off + 4)
                if nxt < 0:
                    break
                off = nxt
                continue
            body = off + 40
            if body + bw_comp > n_total:
                break  # partial tail
            blocks.append((off, bw_comp, bw_count, bw_tag, bw_fts, bw_lts))
            off = body + bw_comp
    dctx = zstd.ZstdDecompressor()
    for off, comp, count, btag, fts, lts in blocks:
        if tag is not None and btag != tag:
            continue
        if t0 is not None and lts < t0:
            continue
        if t1 is not None and fts > t1:
            continue
        (_bm, _bt, _p0, blk_rsz, _cnt, _comp, uncomp, _f, _l) = _BLK_HDR.unpack_from(data, off)
        if rsz is not None and blk_rsz != rsz:
            continue  # stale-width block (schema changed mid-file) — skip
        body = off + 40
        try:
            raw = dctx.decompress(data[body : body + comp], max_output_size=uncomp)
        except Exception:
            continue
        for i in range(count):
            yield raw[i * blk_rsz : (i + 1) * blk_rsz]


def _iter_records(path: str, tag_want: int, rsz_want: int) -> Iterator[bytes]:
    """Back-compat shim: typed helpers call this; delegates to v1 iter_records.
    rsz_want is passed as the record-width filter so blocks of a stale width
    (a mid-file payload-schema change for this tag) are skipped, not mis-unpacked."""
    yield from iter_records(path, tag=tag_want, rsz=rsz_want)


def iter_quotes(path: str, only_changed: bool = True) -> Iterator[QuoteV3]:
    """SpotQuote stream — decodes BOTH TAG_QUOTE_V3=15 (legacy, us) and
    TAG_QUOTE_NS=22 (canonical, ns), yielding ns-scale ts_received_ns/
    ts_wall_ns regardless of which generation a given record came from."""
    for rec, scale in _iter_dual_tag(path, TAG_QUOTE_NS, 64):
        (bid, ask, bsz, asz, ts_rcv, ts_wall, seq, src, qccy, aid, chg) = QUOTE_V3.unpack(rec)
        if only_changed and chg != 1:
            continue
        yield QuoteV3(
            ts_received_ns=ts_rcv * scale,
            ts_wall_ns=ts_wall * scale,
            bid=bid,
            ask=ask,
            bid_size=bsz,
            ask_size=asz,
            seq=seq,
            source=src,
            quote_ccy=qccy,
            asset_id=aid,
            changed=chg,
        )


# NOTE (2026-06-05): some 20260605 BBO files are recorded under TypeTag=1
# (40B) instead of V3 tag-15/64B. The on-disk 40B record does NOT match the
# documented legacy HelixSpotQuoteV1 (bid/ask/ts_pub/ts_rcv/source). Observed
# bytes decode as bid, ask, bid_size, ask_size, <8B source/pad>, with the
# per-record timestamp living in the FRAME header, not the record. The exact
# layout is unconfirmed against the recorder write-site, so a legacy quote
# reader is intentionally NOT shipped here (a guessed layout would silently
# corrupt timestamps). Resolve by confirming what struct the recorder
# serializes under tag 1 for these files before adding iter_quotes_legacy.


def iter_hl_funding(path: str) -> Iterator[HlFunding]:
    """HL perp funding — decodes BOTH TAG_HL_FUNDING=16 (legacy, us) and
    TAG_HL_FUNDING_NS=28 (canonical, ns). One record per (family, ~1/min
    poll). funding_rate>0 => HL short earns it (the user's HL/Lighter carry leg)."""
    for rec, scale in _iter_dual_tag(path, TAG_HL_FUNDING_NS, 56):
        (ts_wall, aid, frate, prem, mark, oracle, oi) = HL_FUNDING.unpack(rec)
        yield HlFunding(
            ts_wall_ns=ts_wall * scale,
            asset_id=aid,
            funding_rate=frate,
            premium=prem,
            mark_px=mark,
            oracle_px=oracle,
            open_interest=oi,
        )


def iter_multi_venue_funding(path: str) -> Iterator[MultiVenueFunding]:
    """Multi-venue perp funding — decodes BOTH TAG_MV_FUNDING=17 (legacy, us)
    and TAG_MV_FUNDING_NS=29 (canonical, ns). One record per (asset, venue,
    ~poll). venue: 0=lighter, 1=hyperliquid, 2=binance, 3=bybit.
    funding_rate>0 => a SHORT earns it (longs pay shorts); <0 => a LONG earns it
    (shorts pay longs) — the sign tells who PAYS whom (the funding-carry leg rule)."""
    for rec, scale in _iter_dual_tag(path, TAG_MV_FUNDING_NS, 24):
        (ts_wall, aid, venue, frate) = MV_FUNDING.unpack(rec)
        yield MultiVenueFunding(
            ts_wall_ns=ts_wall * scale, asset_id=aid, venue=venue, funding_rate=frate
        )


def iter_perp_funding(path: str) -> Iterator[PerpFunding]:
    """Generic per-venue perp funding — decodes BOTH TAG_PERP_FUNDING=19
    (legacy, us) and TAG_PERP_FUNDING_NS=30 (canonical, ns). One record per
    (family, ~1/min poll). Emitted by AsterPerp (venue=30) + Bulk (venue=33)
    feed hosts from their own funding endpoints; carries rate + mark/oracle and
    (Bulk) open_interest. funding_rate>0 => a SHORT earns it. Each poll ALSO
    writes a MultiVenueFunding (venue 4=aster/5=bulk) into the SAME
    *_Funding stream, so iter_multi_venue_funding() sees these too."""
    for rec, scale in _iter_dual_tag(path, TAG_PERP_FUNDING_NS, 56):
        (ts_wall, aid, venue, frate, prem, mark, oracle, oi) = PERP_FUNDING.unpack(rec)
        yield PerpFunding(
            ts_wall_ns=ts_wall * scale,
            asset_id=aid,
            venue=venue,
            funding_rate=frate,
            premium=prem,
            mark_px=mark,
            oracle_px=oracle,
            open_interest=oi,
        )


def iter_daily_bars(path: str) -> Iterator[DailyBar]:
    """Daily OHLCV bars (TypeTag=20, 64B, DailyBar_Lighter src 0x1B00). One record
    per (asset, day); day_unix is 00:00-UTC epoch seconds. Seeded by xsec_backfill,
    grown daily by daily_bar_collector. Rank the cross-sectional momentum on `close`."""
    for rec in _iter_records(path, TAG_DAILY_BAR, 64):
        (day_unix, aid, aw, o, h, lo, c, vb, vq) = DAILY_BAR.unpack(rec)
        aid = aw or aid  # V4: u8 is the 0xFF sentinel when the true id >= 256
        yield DailyBar(
            day_unix=day_unix,
            asset_id=aid,
            open=o,
            high=h,
            low=lo,
            close=c,
            volume_base=vb,
            volume_quote=vq,
        )


def iter_hourly_bars(path: str) -> Iterator[HourlyBar]:
    """Hourly OHLCV bars (TypeTag=21, 64B, HourlyBar_Lighter src 0x1B01 or HourlyBar_Hyperliquid src 0x1B03).
    One record per (asset, hour); hour_unix is top-of-hour epoch seconds. Seeded by
    xsec_hourly_backfill, grown hourly by hourly_bar_collector."""
    for rec in _iter_records(path, TAG_HOURLY_BAR, 64):
        (hour_unix, aid, aw, o, h, lo, c, vb, vq) = HOURLY_BAR.unpack(rec)
        aid = aw or aid  # V4: u8 is the 0xFF sentinel when the true id >= 256
        yield HourlyBar(
            hour_unix=hour_unix,
            asset_id=aid,
            open=o,
            high=h,
            low=lo,
            close=c,
            volume_base=vb,
            volume_quote=vq,
        )


@dataclass
class StablecoinRatio:
    usd_per_usdt: float
    usd_per_usdc: float
    ts_received_ns: int


def iter_stablecoin_ratios(path: str) -> Iterator[StablecoinRatio]:
    """Stablecoin ratios — decodes BOTH TAG_STABLECOIN=10 (legacy, us) and
    TAG_STABLECOIN_NS=31 (canonical, ns). USD per USDT / USDC from Coinbase.
    Own stream (StablecoinRatios/) since 2026-06-07."""
    for rec, scale in _iter_dual_tag(path, TAG_STABLECOIN_NS, 32):
        (usdt, usdc, ts) = STABLECOIN.unpack(rec)
        yield StablecoinRatio(usd_per_usdt=usdt, usd_per_usdc=usdc, ts_received_ns=ts * scale)


def iter_multivenue_funding(path: str) -> Iterator[MultiVenueFunding]:
    """Lighter multi-venue funding — decodes BOTH TAG_MV_FUNDING=17 (legacy,
    us) and TAG_MV_FUNDING_NS=29 (canonical, ns). One record per (family,
    exchange) per ~1/min poll. venue: 0=lighter,1=hyperliquid,2=binance,3=bybit.
    Net HL/Lighter carry = (venue==1 rate) - (venue==0 rate) for a given
    (ts_wall_ns, asset_id). funding_rate>0 => a SHORT earns it."""
    for rec, scale in _iter_dual_tag(path, TAG_MV_FUNDING_NS, 24):
        (ts_wall, aid, venue, frate) = MV_FUNDING.unpack(rec)
        yield MultiVenueFunding(
            ts_wall_ns=ts_wall * scale, asset_id=aid, venue=venue, funding_rate=frate
        )


def iter_kalshi_windows(path: str) -> Iterator[KalshiWindow]:
    """Kalshi window records — decodes BOTH TAG_KALSHI_WINDOW=18 (legacy, us)
    and TAG_KALSHI_WINDOW_NS=34 (canonical, ns). One record per (asset,
    market_ticker) on first-sight / 15-min rotation. Carries the FULL Kalshi
    ticker + strike + window open_unix parsed from it, so a single *15m
    asset_id cell's BBO/trade records can be split back into the individual
    15-min strike contracts offline (settlement / cross-venue join)."""
    for rec, scale in _iter_dual_tag(path, TAG_KALSHI_WINDOW_NS, 80):
        (ts_rec, open_unix, ticker_raw, strike, aid) = KALSHI_WINDOW.unpack(rec)
        ticker = ticker_raw.split(b"\x00", 1)[0].decode("ascii", "replace")
        yield KalshiWindow(
            ts_recorded_ns=ts_rec * scale,
            open_unix=open_unix,
            market_ticker=ticker,
            strike=strike,
            asset_id=aid,
        )


def iter_poly_windows(path: str) -> Iterator[PolyWindow]:
    """Polymarket window records — decodes BOTH TAG_POLY_WINDOW=7 (legacy, us)
    and TAG_POLY_WINDOW_NS=32 (canonical, ns). Maps a Poly 15m window to its
    YES/NO CLOB token ids + strike — the cross-venue join key against
    KalshiWindow's (asset, open_unix, strike). token_yes/token_no key the
    Polymarket_CLOB_{Yes,No}_BBO streams for the right contract."""

    def _s(b: bytes) -> str:
        return b.split(b"\x00", 1)[0].decode("ascii", "replace")

    for rec, scale in _iter_dual_tag(path, TAG_POLY_WINDOW_NS, 256):
        (ts_rec, open_unix, cond, tyes, tno, strike, asset) = POLY_WINDOW.unpack(rec)
        yield PolyWindow(
            ts_recorded_ns=ts_rec * scale,
            open_unix=open_unix,
            condition_id=_s(cond),
            token_yes=_s(tyes),
            token_no=_s(tno),
            strike=strike,
            asset=asset,
        )


def iter_book(path: str, asset_id: Optional[int] = None) -> Iterator[BookL2]:
    """Lighter_Book L2 snapshots, 688B, 20 levels/side, streaming (memory-safe).
    Decodes BOTH TAG_LIGHTER_BOOK=2 (legacy, us metadata-trailer ts) and
    TAG_BOOK_NS=23 (canonical, ns), yielding ns-scale ts_received_ns regardless
    of generation. The feed MULTIPLEXES all asset families under one source id
    with no per-record tag on pre-V3 recordings, so asset_id falls back to
    price-band inference (_book_asset_of) there. Pass asset_id to filter to one
    family (NOTE: SOL=17 and HYPE=21 share a price band on 20260603 — the filter
    returns SOL's id for both; disambiguate via the BBO/trade feed if needed)."""
    # v1: per-record book payload is the bare 688B of (price,size) pairs plus a
    # metadata trailer (asset_id, ts_received, source -- see _BOOK_OFF_*
    # constants above). The block's source_id is the file-level source_id;
    # this ALSO reads the file header once for the fallback `source` value in
    # case the trailer byte is legacy-unstamped (0).
    src = TAG_LIGHTER_BOOK
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
        if len(head) >= 32:
            (_m, _v, _h, src, _r, _c, _f, _p) = _FILE_HDR.unpack_from(head, 0)
    except Exception:
        pass
    for rec, scale in _iter_dual_tag(path, TAG_BOOK_NS, 688):
        if len(rec) != 688:
            continue
        d = _BOOK_PAIRS.unpack(rec)
        bids = [
            (d[2 * k], d[2 * k + 1]) for k in range(0, 20) if d[2 * k] > 0.0 and d[2 * k + 1] > 0.0
        ]
        asks = [
            (d[2 * k], d[2 * k + 1]) for k in range(20, 40) if d[2 * k] > 0.0 and d[2 * k + 1] > 0.0
        ]
        if not bids:
            continue
        # Orderbook V3 (2026-06-06): asset_id is stamped in the record's metadata
        # trailer (offset 673). Use it when present; fall back to the legacy
        # price-band inference for pre-V3 recordings (asset_id byte == 0).
        rec_aid = rec[_BOOK_OFF_ASSET_ID]
        aid = rec_aid if rec_aid != 0 else _book_asset_of(bids[0][0])
        if asset_id is not None and aid != asset_id:
            continue
        ts_raw = int.from_bytes(
            rec[_BOOK_OFF_TS_RECV : _BOOK_OFF_TS_RECV + 8], "little", signed=True
        )
        yield BookL2(ts_received_ns=ts_raw * scale, asset_id=aid, source=src, bids=bids, asks=asks)


def iter_trades(path: str, asset_id: Optional[int] = None) -> Iterator[TradeV3]:
    """Iterate recorded trade prints — decodes BOTH TAG_TRADE=3 (legacy, us)
    and TAG_TRADE_NS=24 (canonical, ns). If asset_id is given, yield only that
    family — no more price-band hack (V3 carries the frozen AssetId index)."""
    for rec, scale in _iter_dual_tag(path, TAG_TRADE_NS, 72):
        (_sym, side, price, size, tid, ts_rcv, ts_wall, src, aid) = TRADE.unpack(rec)
        s = side.decode("ascii", "replace")
        if s not in ("B", "S"):
            continue
        if asset_id is not None and aid != asset_id:
            continue
        yield TradeV3(
            ts_received_ns=ts_rcv * scale,
            ts_wall_ns=ts_wall * scale,
            side=s,
            price=price,
            size=size,
            trade_id=tid,
            source=src,
            asset_id=aid,
        )


def decode_quotes(
    path: str, only_changed: bool = True, limit: Optional[int] = None
) -> List[QuoteV3]:
    """BOUNDED convenience wrapper. Prefer iter_quotes for full-day work."""
    gen = iter_quotes(path, only_changed=only_changed)
    if limit is not None:
        gen = itertools.islice(gen, limit)
    return list(gen)


def decode_trades(path: str, limit: Optional[int] = None) -> List[TradeV3]:
    gen = iter_trades(path)
    if limit is not None:
        gen = itertools.islice(gen, limit)
    return list(gen)


def iter_poly_bbo(path: str) -> Iterator[PolyClobBbo]:
    """Decodes BOTH TAG_POLY_BBO=14 (legacy, us) and TAG_POLY_BBO_NS=27
    (canonical, ns). px/sz fields are e8-scaled fixed point (unaffected by the
    time-plane scale)."""
    for rec, scale in _iter_dual_tag(path, TAG_POLY_BBO_NS, 224):
        (ts_rec, ts_wall, ts_pub, bid_px, bid_sz, ask_px, ask_sz, cond, token, outcome) = (
            POLY_BBO.unpack(rec)
        )
        yield PolyClobBbo(
            ts_received_ns=ts_rec * scale,
            ts_publisher_ns=ts_pub * scale,
            bid_px=bid_px / _E8,
            bid_sz=bid_sz / _E8,
            ask_px=ask_px / _E8,
            ask_sz=ask_sz / _E8,
            outcome=outcome,
            condition_id=cond.split(b"\x00", 1)[0].decode("ascii", "replace"),
            token_id=token.split(b"\x00", 1)[0].decode("ascii", "replace"),
            ts_wall_ns=ts_wall * scale,
        )


def iter_poly_trades(path: str) -> Iterator[PolyClobTrade]:
    """Decodes BOTH TAG_POLY_TRADE=12 (legacy, us) and TAG_POLY_TRADE_NS=25
    (canonical, ns)."""
    for rec, scale in _iter_dual_tag(path, TAG_POLY_TRADE_NS, 208):
        (cond, asset, side, price, size, ts_pub, ts_rec, seq) = POLY_TRADE.unpack(rec)
        s = side.decode("ascii", "replace")
        yield PolyClobTrade(
            ts_received_ns=ts_rec * scale,
            ts_publisher_ns=ts_pub * scale,
            side=s,
            price=price,
            size=size,
            condition_id=cond.split(b"\x00", 1)[0].decode("ascii", "replace"),
            asset_id=asset.split(b"\x00", 1)[0].decode("ascii", "replace"),
            seq=seq,
        )


def decode_poly_bbo(path: str, limit: Optional[int] = None) -> List[PolyClobBbo]:
    gen = iter_poly_bbo(path)
    if limit is not None:
        gen = itertools.islice(gen, limit)
    return list(gen)


def decode_poly_trades(path: str, limit: Optional[int] = None) -> List[PolyClobTrade]:
    gen = iter_poly_trades(path)
    if limit is not None:
        gen = itertools.islice(gen, limit)
    return list(gen)
