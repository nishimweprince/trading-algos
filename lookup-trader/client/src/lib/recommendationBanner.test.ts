import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { RecommendationBanner } from "@/components/trade/RecommendationBanner";
import type { RecommendationPayload, RecommendationVerdict } from "@/types";

const cases: Array<[RecommendationVerdict, string]> = [
  ["buy", "Buy"],
  ["sell", "Sell"],
  ["lean_long", "Lean long"],
  ["lean_short", "Lean short"],
  ["wait", "Wait"],
  ["insufficient_data", "Insufficient data"],
];

describe("recommendation presentation", () => {
  it.each(cases)("renders %s without changing the evidence wording", (verdict, headline) => {
    const recommendation: RecommendationPayload = {
      verdict,
      headline,
      rationale: "Evidence rationale",
      caveats: [],
      policy_version: "empirical-block-bootstrap-v2",
    };
    const html = renderToStaticMarkup(
      createElement(RecommendationBanner, {
        recommendation,
        side: verdict === "sell" || verdict === "lean_short" ? -1 : 1,
        horizon: 24,
        targetAtr: 1.5,
        stopAtr: 1,
      }),
    );
    expect(html).toContain(headline);
    expect(html).toContain("Evidence rationale");
    expect(html).toContain("Past bars only");
  });
});
