import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import ThinkingChain from "@/components/ThinkingChain";

describe("ThinkingChain", () => {
  it("renders steps count", () => {
    render(
      <ThinkingChain
        events={[
          { name: "get_weather", status: "ok", latency_ms: 200 },
          { name: "search_restaurant_kb", status: "running" },
        ]}
      />
    );
    expect(screen.getByText(/思考过程 · 2 步/)).toBeTruthy();
  });
});
