import { describe, expect, it } from "vitest";

import {
  AUTO_FILL_MIN_CONFIDENCE,
  autoFillTag,
  baseRateTag,
  completeTags,
  tagHint,
} from "./barTags";
import type { BarTag } from "@/types";

const tag = (over: Partial<BarTag> = {}): BarTag => ({
  setup_id: "bull_engulfing",
  state: "complete",
  confidence: 0.9,
  source: "rule",
  side: 1,
  model_version: null,
  ...over,
});

describe("completeTags", () => {
  it("filters on state, not on confidence", () => {
    const tags = [tag({ confidence: 0.61 }), tag({ setup_id: "double_bottom", state: "forming" })];
    expect(completeTags(tags).map((t) => t.setup_id)).toEqual(["bull_engulfing"]);
  });

  it("treats undefined as empty", () => {
    expect(completeTags(undefined)).toEqual([]);
  });
});

describe("autoFillTag", () => {
  it("returns nothing when there are no tags", () => {
    expect(autoFillTag(undefined)).toBeNull();
    expect(autoFillTag([])).toBeNull();
  });

  it("fills from a single confident complete tag", () => {
    expect(autoFillTag([tag({ confidence: 0.9 })])?.setup_id).toBe("bull_engulfing");
  });

  it("declines a marginal match", () => {
    expect(autoFillTag([tag({ confidence: 0.61 })])).toBeNull();
  });

  it("declines when two tags both clear the bar", () => {
    // An ambiguous bar is a real reading, not a tie to be broken.
    expect(
      autoFillTag([tag({ confidence: 0.95 }), tag({ setup_id: "pin_bar_long", confidence: 0.9 })]),
    ).toBeNull();
  });

  it("declines when the two readings disagree about direction", () => {
    expect(
      autoFillTag([
        tag({ setup_id: "bear_engulfing", side: -1, confidence: 0.9 }),
        tag({ setup_id: "pin_bar_long", side: 1, confidence: 0.85 }),
      ]),
    ).toBeNull();
  });

  it("ignores a forming structure however confident it is", () => {
    expect(
      autoFillTag([tag({ confidence: 0.8 }), tag({ setup_id: "double_bottom", state: "forming", confidence: 0.99 })])
        ?.setup_id,
    ).toBe("bull_engulfing");
  });

  it("returns nothing when only forming structures are present", () => {
    expect(autoFillTag([tag({ state: "forming", confidence: 0.99 })])).toBeNull();
  });

  it("treats the threshold as inclusive", () => {
    expect(autoFillTag([tag({ confidence: AUTO_FILL_MIN_CONFIDENCE })])).not.toBeNull();
  });

  it("honours an overridden threshold", () => {
    expect(autoFillTag([tag({ confidence: 0.61 })], 0.6)?.setup_id).toBe("bull_engulfing");
  });
});

describe("baseRateTag", () => {
  const tags = [
    tag({ setup_id: "bull_engulfing" }),
    tag({ setup_id: "double_bottom", state: "forming", source: "algorithm" }),
  ];

  it("uses the selected complete tag", () => {
    expect(baseRateTag(tags, "bull_engulfing", null)?.setup_id).toBe("bull_engulfing");
  });

  it("falls back to the current primary when no setup is selected", () => {
    expect(baseRateTag(tags, null, "bull_engulfing")?.setup_id).toBe("bull_engulfing");
  });

  it("never conditions a base rate on a forming tag", () => {
    expect(baseRateTag(tags, "double_bottom", "bull_engulfing")).toBeNull();
  });
});

describe("tagHint", () => {
  it("says nothing when there is nothing to explain", () => {
    expect(tagHint(undefined)).toBeNull();
    expect(tagHint([tag({ confidence: 0.9 })])).toBeNull();
  });

  it("explains an ambiguous bar", () => {
    const hint = tagHint([tag({ confidence: 0.9 }), tag({ setup_id: "pin_bar_long", confidence: 0.8 })]);
    expect(hint).toContain("2 patterns");
  });

  it("explains a marginal match", () => {
    expect(tagHint([tag({ confidence: 0.61 })])).toContain("Marginal match");
  });

  it("stays silent for forming-only bars, which the chips already show", () => {
    expect(tagHint([tag({ state: "forming", confidence: 0.99 })])).toBeNull();
  });
});
