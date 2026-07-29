// Fixture: deliberately slop-ridden TS test suite for slopguard's tests.
import { describe, expect, it, vi } from "vitest";

declare function formatPrice(cents: number): string;

describe("slop suite", () => {
  it("runs without crashing", async () => {
    const svc = { submit: vi.fn() };
    await svc.submit({ id: 1 });
    // no-assert-test: nothing checked
  });

  it("calls the gateway", () => {
    const gateway = { charge: vi.fn() };
    gateway.charge(100);
    expect(gateway.charge).toHaveBeenCalledWith(100); // mock-only-test
  });

  it("is always true", () => {
    expect(true).toBe(true); // tautological-assert
  });

  it("waits for readiness", async () => {
    await new Promise((resolve) => setTimeout(resolve, 2000)); // sleep-in-test
    expect(formatPrice(0)).toBeDefined();
  });

  it("pins the exact rendered sentence", () => {
    expect(formatPrice(199)).toBe(
      "Your total comes to exactly $1.99 including all applicable taxes and fees today" // brittle-exact-string
    );
  });

  // parametrize-candidate: three blocks identical except literals
  it("formats one dollar", () => {
    const out = formatPrice(100);
    expect(out).toBe("$1.00");
  });

  it("formats two dollars", () => {
    const out = formatPrice(200);
    expect(out).toBe("$2.00");
  });

  it("formats three dollars", () => {
    const out = formatPrice(300);
    expect(out).toBe("$3.00");
  });
});
