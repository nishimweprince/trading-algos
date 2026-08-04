# Plan: Bar-level pattern tagging and ComparePanel integration

**Status:** Draft — pick up for implementation  
**Created:** 2026-08-04  
**Scope:** Precompute pattern/setup tags on every closed candle, persist in `bar_features`, and wire `ComparePanel` + comparison services to use them.

This plan moves **automated pattern detection** from README “Phase 2+ / out of scope” into a concrete, shippable feature. It is designed to be executed in phases; Phase 1 is the minimum viable path for tomorrow’s session.

---

## 1. Problem statement

Today the system has two disconnected notions of “pattern”:

| Layer | What exists | Gap |
|-------|-------------|-----|
| **Operator labels** | `occurrences.setup_id` from manual replay labelling | Sparse, biased, slow to accumulate |
| **Bar context** | `bar_features` trend/session/RSI/ATR/shape | No pattern vocabulary (engulfing, double bottom, H&S, etc.) |
| **Compare** | Filters `occurrences` by `setup_id` + context dimensions | Cannot answer “what happened after every bar that looked like this pattern in this context” |
| **Base rate** | Filters all historical bars by context only | Cannot condition on pattern tags at the signal bar |

The screenshot “Wait / 50% base rate / n=16” read is **context-only**. It does not know the bar is (or isn’t) an engulfing bar, part of a double-bottom formation, etc.

**Goal:** For every closed candle, store **which patterns are present** (and optionally **forming vs complete**), then let `ComparePanel` auto-surface tags at the cursor and use them in base-rate and compare queries.

---

## 2. Design principles

1. **Causal only** — tags at bar `t` may use bars `≤ t` only (same invariant as `context_half` in `bar_features.py`).
2. **Versioned & rebuildable** — tags bump `bar_feature_version` (or a dedicated `tag_version`); no silent drift.
3. **Hybrid tagging** — deterministic rules for simple candlestick patterns; LLM only where rules are weak (chart patterns).
4. **Multi-label** — a bar can carry several tags (e.g. `bull_engulfing` + `forming_double_bottom`).
5. **Provenance** — every tag records `source` (`rule` | `llm`), `confidence`, `model_version`, `prompt_version`.
6. **Separation of concerns** — tagging pipeline is offline; API reads precomputed columns only (no live OpenAI in `/compare`).
7. **Backward compatible** — existing occurrence-based compare unchanged; bar tags add new dimensions and auto-fill.

---

## 3. Tag taxonomy

Align with [`server/app/db/setups_seed.py`](../server/app/db/setups_seed.py) (35 setups). Group by tagging strategy:

### 3.1 Rule-based (Phase 1 — no API cost)

| Category | Setup IDs | Lookback | Notes |
|----------|-----------|----------|-------|
| Candlestick | `bull_engulfing`, `bear_engulfing`, `pin_bar_long`, `inside_break` | 2–5 bars | OHLC geometry |
| Simple structure | `key_level_approach`, `key_level_breakout` | 20–40 bars | Distance to swing / round number (partially in `bar_features` today) |

### 3.2 Algorithmic / shape-based (Phase 2)

| Category | Setup IDs | Lookback | Notes |
|----------|-----------|----------|-------|
| Chart patterns | `double_top`, `double_bottom`, `head_shoulders`, `inv_head_shoulders`, triangles, wedges, flags | 40–120 bars | Swing detection + template matching on `shape_48` / swings |
| Fibonacci | `fib_abcd`, `fib_gartley`, etc. | 50–150 bars | Pivot ratios |

### 3.3 LLM-assisted (Phase 3 — optional, ~$1–12 per 20k bars)

Use OpenAI only for **ambiguous chart patterns** where rule confidence &lt; threshold. Input: compact OHLC window + existing `bar_features` summary (not chart images).

**Estimated cost (20,000 bars, GPT-4o-mini, batched OHLC):** ~$1–5 one-time per symbol/timeframe rebuild.

---

## 4. Data model

### 4.1 Storage format (recommended)

Add to each `bar_features` row:

```json
{
  "bar_tags": {
    "version": "1.0.0",
    "tags": [
      {
        "setup_id": "bull_engulfing",
        "state": "complete",
        "confidence": 0.95,
        "source": "rule",
        "model_version": null
      },
      {
        "setup_id": "double_bottom",
        "state": "forming",
        "confidence": 0.72,
        "source": "llm",
        "model_version": "gpt-4o-mini-2026-08"
      }
    ]
  }
}
```

**Parquet column:** `bar_tags VARCHAR` (JSON string), same pattern as `level_touch`.

**Denormalized indexes for SQL** (optional but recommended for DuckDB filters):

| Column | Type | Purpose |
|--------|------|---------|
| `tag_setup_ids` | `VARCHAR` | Comma-separated active `setup_id` list for `list_contains` |
| `tag_primary_setup_id` | `VARCHAR` | Highest-confidence complete tag (for simple UI) |
| `tag_count` | `INT` | Number of tags on bar |

Bump `settings.bar_feature_version` when tag schema or tagger logic changes.

### 4.2 Tag states

| State | Meaning |
|-------|---------|
| `complete` | Pattern fully formed at this bar’s close |
| `forming` | Structure in progress (e.g. second shoulder not yet in) |
| `invalidated` | Was forming; structure broke (optional, for audit) |

### 4.3 Relationship to occurrences

`occurrences` already has `tagger_confidence`, `tagger_model_version`, `payload_hash` ([`schema.sql`](../server/app/db/schema.sql)). Bar tags are the **universe**; occurrences are **operator-confirmed subset**. Future work can compare “LLM said engulfing” vs “operator labelled engulfing” for calibration.

---

## 5. Pipeline architecture

```mermaid
flowchart LR
  subgraph offline [Offline build]
    Candles[candles Parquet]
    BF[build_bar_features.py]
    Rules[taggers/rules.py]
    Algo[taggers/chart.py]
    LLM[taggers/llm_batch.py]
    Store[features Parquet]
    Candles --> BF
    BF --> Rules
    Rules --> Algo
    Algo --> LLM
    LLM --> Store
  end

  subgraph runtime [API / UI]
    API[/context /base-rate /compare]
    Panel[ComparePanel.tsx]
    Store --> API
    API --> Panel
  end
```

### 5.1 New modules (server)

```
server/app/taggers/
  __init__.py
  types.py           # BarTag, TagResult, TagState
  rules/
    engulfing.py
    pin_bar.py
    inside_bar.py
  chart/
    swings.py        # reuse swing_indices from context
    double_bottom.py
    head_shoulders.py
  llm/
    prompt.py
    batch.py         # chunked OpenAI calls
  pipeline.py        # orchestrate per-bar tagging
```

### 5.2 Integration point in builder

[`scripts/build_bar_features.py`](../scripts/build_bar_features.py) today:

```python
row = bf.compute_bar_row(window, forward, symbol, timeframe, pip, htf)
row["level_touch"] = json.dumps(...)
```

**After:**

```python
row = bf.compute_bar_row(...)
row["level_touch"] = json.dumps(...)
tag_result = tag_pipeline.tag_bar(window, row, symbol, timeframe)
row["bar_tags"] = json.dumps(tag_result.to_json())
row["tag_setup_ids"] = ",".join(tag_result.setup_ids())
row["tag_primary_setup_id"] = tag_result.primary_setup_id()
row["tag_count"] = len(tag_result.tags)
```

Tagging runs **after** `context_half` so taggers can read `atr_at_bar`, swings, shape, etc.

### 5.3 Incremental rebuild

Extend `_bars_to_build` logic:

- Rebuild rows when `bar_feature_version` changes (already true).
- Rebuild when `tag_version` changes even if forward half is complete.
- LLM pass can be **skipped** if `bar_tags` exists and `tag_version` matches (rules-only rebuild is cheap).

### 5.4 Separate script (optional)

`scripts/tag_bar_features.py` — run LLM pass over existing parquet without recomputing forward half. Useful for iterating prompts without a full feature rebuild.

---

## 6. Server API changes

### 6.1 `GET /context`

Extend [`server/app/routers/context.py`](../server/app/routers/context.py) response:

```python
{
  # ... existing fields ...
  "bar_tags": [...],           # parsed from store row at signal_ts
  "tag_primary_setup_id": "bull_engulfing" | null,
}
```

Read from `bar_features` row at `signal_ts` when store exists; fallback `[]` if missing.

### 6.2 `GET /base-rate`

Extend [`server/app/services/base_rate.py`](../server/app/services/base_rate.py):

**New optional query params:**

- `setup_id` or `tag_setup_id` — filter population to bars where `list_contains(tag_setup_ids, setup_id)`
- `tag_state` — `complete` | `forming` | `any` (default `complete`)

**Relaxation ladder** — add to `BAR_RELAX_ORDER` (dropped early, narrow):

```python
BAR_RELAX_ORDER = [
    "tag_setup_id",      # NEW — most specific
    "session_overlap",
    # ... existing ...
]
```

Implementation: `_SPECIAL_CLAUSES` style:

```sql
list_contains(str_split(tag_setup_ids, ','), ?)
```

**Important:** Base rate with tag filter answers:  
*“Among historical bars that had this pattern in this context, what did price do next?”*  
This is distinct from occurrence compare.

### 6.3 `POST /compare`

Two modes (can coexist):

| Mode | Population | When to use |
|------|------------|-------------|
| **A. Occurrence** (current) | Labelled `occurrences` for `setup_id` | Operator trust, blinded lab data |
| **B. Bar-tag** (new) | All `bar_features` rows with matching tag | Large sample from history |

**Option B** may be a new endpoint `POST /compare-bars` or a flag `compare_source: "occurrences" | "bar_tags"`.

Extend `CompareContext`:

```typescript
tag_setup_ids?: string[];      // match any (OR) or all (AND) — decide in impl
tag_state?: "complete" | "forming" | "any";
```

Wire `compare_with_recommendation` to bar-tag population when `compare_source=bar_tags`.

### 6.4 `GET /setups` enrichment

Return `category`, `default_side`, `taggable_by` (`rule` | `algorithm` | `llm`) for UI grouping.

---

## 7. ComparePanel changes

File: [`client/src/components/trade/ComparePanel.tsx`](../client/src/components/trade/ComparePanel.tsx)

### 7.1 Auto-fill from bar tags (cursor-driven)

When `signalContext` updates:

1. Parse `bar_tags` from `/context`.
2. **Setup field:** if exactly one `complete` tag with confidence ≥ threshold → `setIfClean("setup_id", tag.setup_id)`.
3. If multiple tags → show chip list “Patterns on this bar” (read-only or selectable).
4. If `forming` only → helper text: “Double bottom forming — not complete.”

### 7.2 Tag-aware base rate block

Extend base-rate query:

```typescript
baseRateQuery = {
  ...existing,
  tagSetupId: primaryTag?.setup_id,  // when user selects or auto-filled
  tagState: "complete",
};
```

Banner subtitle: “Base rate for bars tagged **Bullish Engulfing** in this context.”

### 7.3 Compare submit

When operator runs compare:

- If `setup_id` matches a tag on the current bar → occurrence compare (default).
- Toggle or auto-switch: **“Compare from bar history”** uses `compare_source=bar_tags` (larger n).
- Show both results side-by-side when sample sizes differ materially.

### 7.4 UI components (new)

| Component | Role |
|-----------|------|
| `BarTagsChips` | Display tags at cursor with state badges (complete/forming) |
| `TagCompareToggle` | Occurrences vs bar-history compare |
| `TagConfidenceHint` | Tooltip: rule vs LLM, model version |

### 7.5 Types

Extend [`client/src/types/index.ts`](../client/src/types/index.ts):

```typescript
export interface BarTag {
  setup_id: string;
  state: "complete" | "forming" | "invalidated";
  confidence: number;
  source: "rule" | "algorithm" | "llm";
  model_version?: string | null;
}

export interface SignalContext {
  // ...existing...
  bar_tags?: BarTag[];
  tag_primary_setup_id?: string | null;
}
```

---

## 8. Rule implementations (Phase 1 detail)

### 8.1 Bullish engulfing

At bar `i` (anchor):

- `close[i] > open[i]` (bullish body)
- `close[i-1] < open[i-1]` (prior bearish)
- `open[i] <= close[i-1]` and `close[i] >= open[i-1]` (body engulfs prior body)
- Optional: `body[i] > body[i-1] * 1.0`

Mirror for `bear_engulfing`.

### 8.2 Pin bar

- Lower wick ≥ 2× body, upper wick small, close in upper third of range (`signal_body_pct` / range already in context).

### 8.3 Inside bar

- `high[i] < high[i-1]` and `low[i] > low[i-1]`; `inside_break` = inside bar + next bar breaks mother high/low (tag on break bar, not inside bar).

### 8.4 Tests

Golden fixtures in `server/tests/fixtures/tagging/` with known OHLC windows and expected tags.

---

## 9. LLM batch design (Phase 3)

### 9.1 When to call

Only if:

- No `complete` rule/algorithm tag for chart-pattern category, AND
- `chart_candidate_score` &gt; threshold (e.g. swing structure resembles H&S skeleton)

### 9.2 Request shape

- **Input:** ~80 bars compact OHLC + `trend_state`, `atr_bucket`, `shape_48` summary (~2–3.5k tokens/chunk).
- **Output:** JSON array of `{setup_id, state, confidence}` per bar in chunk.
- **Batch:** 100 bars per request → ~200 requests for 20k bars.

### 9.3 Prompt contract

- Closed vocabulary = `setups_seed` IDs only.
- Must return `[]` if no pattern.
- Distinguish `forming` vs `complete` explicitly.
- No forward data (system prompt enforces causal window).

### 9.4 Cost guardrails

- `LOOKUP_TAG_LLM_MAX_BARS` env cap per run.
- Dry-run mode: count tokens, no API calls.
- Cache responses by `payload_hash` (column already exists on occurrences; reuse pattern).

---

## 10. Testing strategy

| Layer | Tests |
|-------|-------|
| Rule taggers | Unit: golden OHLC → expected tags |
| Pipeline | Integration: `compute_bar_row` + tags → parquet round-trip |
| Parity | Tag at build time == tag from live `/context` at same `ts` |
| Base rate | Filter by `tag_setup_id` returns subset of untagged population |
| Compare | Bar-tag compare n ≥ occurrence compare for same pattern |
| ComparePanel | Auto-fill setup when single high-confidence tag present |
| Leakage | Taggers never read `forward` half |

---

## 11. Migration & rollout

1. **Bump `bar_feature_version`** to `1.1.0` (or `2.0.0` if breaking).
2. Run `python3 scripts/build_bar_features.py --symbol XAUUSD --timeframe H1 --rebuild` (or incremental).
3. Verify `/context` returns tags for sample bars.
4. Ship ComparePanel read-only tag display (no compare change).
5. Enable tag-filtered base rate behind feature flag `LOOKUP_BAR_TAGS=1`.
6. Enable bar-tag compare mode.

**README update:** Remove “Automated pattern detection” from out-of-scope once Phase 1 ships.

---

## 12. Phased implementation (pick up tomorrow)

### Phase 1 — Rules + storage + UI display (1–2 days)

**Goal:** Tags exist in parquet; operator sees them at cursor.

- [ ] `server/app/taggers/` with rule taggers for 4 candlestick setups
- [ ] `bar_tags`, `tag_setup_ids`, `tag_primary_setup_id`, `tag_count` columns
- [ ] Wire into `build_bar_features.py` + bump `bar_feature_version`
- [ ] `/context` returns `bar_tags`
- [ ] ComparePanel: `BarTagsChips` + auto-fill `setup_id` when unambiguous
- [ ] Unit tests for rules + builder integration

**Not in Phase 1:** LLM, bar-tag compare endpoint, base-rate tag filter.

### Phase 2 — Tag-filtered base rate (1 day)

- [ ] `GET /base-rate?tag_setup_id=bull_engulfing&tag_state=complete`
- [ ] `BAR_RELAX_ORDER` + SQL clause for tags
- [ ] ComparePanel passes selected tag into `useBaseRate`
- [ ] Copy updates: “base rate for engulfing bars in this context”

### Phase 3 — Bar-tag compare mode (1–2 days)

- [ ] `POST /compare` with `compare_source=bar_tags` OR new `/compare-bars`
- [ ] Reuse `compare_with_recommendation` over bar population + `outcome_expr`
- [ ] ComparePanel toggle + dual results display
- [ ] Server tests for sample size vs occurrence compare

### Phase 4 — Chart patterns + LLM (2–4 days)

- [ ] Algorithmic taggers for double bottom, H&S (minimum viable)
- [ ] `scripts/tag_bar_features.py` LLM batch script
- [ ] Prompt versioning + `payload_hash` cache
- [ ] QA sample review UI or export CSV for manual audit

### Phase 5 — Calibration & operator feedback (ongoing)

- [ ] Compare bar tags vs operator `setup_id` on occurrences
- [ ] Confusion matrix per pattern
- [ ] Tune confidence thresholds; retrain prompts

---

## 13. Open questions (decide before Phase 2)

1. **OR vs AND** when multiple `tag_setup_ids` in compare filter?
   - Recommend: **OR** for “any of these patterns”, **AND** via confluence-style multi-select later.
2. **Forming tags in base rate?** Default **exclude** `forming` (only `complete` counts) unless operator opts in.
3. **HTF patterns?** Tag on LTF bar using LTF window only for v1; HTF pattern tags deferred.
4. **Same bar, multiple complete tags?** Allow multi-label; `tag_primary_setup_id` = highest confidence complete tag.
5. **Blinded sessions:** Show tags (they’re causal) but keep base-rate withholding behavior unchanged.

---

## 14. Files to touch (checklist)

| File | Change |
|------|--------|
| [`scripts/build_bar_features.py`](../scripts/build_bar_features.py) | Call tag pipeline; new columns; coerce dtypes |
| [`server/app/services/bar_features.py`](../server/app/services/bar_features.py) | Optional: `tags_half()` helper |
| [`server/app/config.py`](../server/app/config.py) | `tag_version`, LLM caps, confidence thresholds |
| [`server/app/routers/context.py`](../server/app/routers/context.py) | Return bar tags from store |
| [`server/app/services/base_rate.py`](../server/app/services/base_rate.py) | Tag filter + relax order |
| [`server/app/services/compare.py`](../server/app/services/compare.py) | Bar-tag population mode |
| [`server/app/models/trade.py`](../server/app/models/trade.py) | `CompareRequest` extensions |
| [`client/src/types/index.ts`](../client/src/types/index.ts) | `BarTag`, context fields |
| [`client/src/lib/api.ts`](../client/src/lib/api.ts) | Query params for tag filters |
| [`client/src/components/trade/ComparePanel.tsx`](../client/src/components/trade/ComparePanel.tsx) | Tags UI, auto-fill, tag-aware queries |
| [`server/tests/test_bar_features.py`](../server/tests/test_bar_features.py) | Tag parity / no leakage |
| **New** `server/tests/test_taggers.py` | Rule golden tests |
| **New** `server/app/taggers/**` | Tagging implementation |
| [`README.md`](../README.md) | Phase 2 scope, build instructions |

---

## 15. Success criteria

- [ ] Rebuild 20k+ bars with tags in &lt; 5 minutes (rules only, no LLM).
- [ ] `/context` at replay cursor shows correct engulfing tag on known fixture bars.
- [ ] ComparePanel auto-selects setup when one high-confidence complete tag present.
- [ ] Tag-filtered base rate n ≤ untagged base rate n for same context (monotonic narrowing).
- [ ] No tagger reads forward candles; leakage tests pass.
- [ ] `bar_feature_version` bump triggers clean rebuild without manual parquet surgery.

---

## 16. Reference: current compare dimensions

Occurrence compare relaxes ([`compare.py`](../server/app/services/compare.py) `RELAX_ORDER`):  
`confidence_min`, `entry_quality`, `confluence_tags`, `consolidation_before`, `at_key_level`, `level_type`, `market_structure`, `htf_alignment`, `observed_trend`, `calendar_flag`, `entry_convention`, `day_of_week`, `htf_trend_state`, `ema_slope_bucket`, `atr_change_bucket`, `rsi_band`, `sl_atr_bucket`, `rr_bucket`, `atr_bucket`, `session`, `trend_state`, `side`.

Bar-tag compare will use a **subset** of these (machine-computed only) plus `tag_setup_id`. Operator-only dimensions (`observed_trend`, `entry_quality`, etc.) apply only to occurrence mode.

---

*Next session: start with **Phase 1** — implement rule taggers and wire `build_bar_features.py`, then surface tags in ComparePanel before changing compare semantics.*
