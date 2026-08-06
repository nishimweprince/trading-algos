import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ModelShadowReadout } from "@/components/trade/ModelShadowReadout";
import { formatShadowDirection } from "@/lib/modelShadow";
import type { OutcomeShadow } from "@/types";

const fixture: OutcomeShadow = {
  long: { direction: "long", side: 1, p_win: 0.612, p_loss: 0.238, p_timeout: 0.15 },
  short: { direction: "short", side: -1, p_win: 0.291, p_loss: 0.509, p_timeout: 0.2 },
  model_version: "hist-gradient-boosting",
  artifact_version: "r2",
  schema_sha256: "abc",
  outcome_feature_version: "1",
  feature_version: "2.0.0",
  bar_feature_version: "1.2.0",
  status: "pilot_shadow",
  pilot: true,
  promoted: false,
};

describe("model shadow readout", () => {
  it("formats all three probabilities for both directions", () => {
    expect(formatShadowDirection(fixture.long)).toBe("Long  W 61.2%  L 23.8%  T 15.0%");
    expect(formatShadowDirection(fixture.short)).toBe("Short  W 29.1%  L 50.9%  T 20.0%");
  });

  it("renders an explicit unpromoted warning and no recommendation", () => {
    const html = renderToStaticMarkup(
      createElement(ModelShadowReadout, { result: fixture, error: null, loading: false }),
    );
    expect(html).toContain("Model shadow");
    expect(html).toContain("pilot · unpromoted");
    expect(html).toContain("Not used by recommendation logic");
    expect(html).toContain("Long  W 61.2%");
    expect(html).toContain("Short  W 29.1%");
    expect(html).not.toContain("Buy");
    expect(html).not.toContain("Sell");
  });
});
