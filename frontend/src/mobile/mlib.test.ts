import { describe, expect, it } from "vitest";
import { parseWagonQr } from "./mlib";

describe("QR-код вагона", () => {
  it("принимает только номер вагона, ссылки не открываются", () => {
    expect(parseWagonQr("DS-WAGON:59900011")).toBe("59900011");
    expect(parseWagonQr(" 52001234 ")).toBe("52001234");
    expect(parseWagonQr("https://evil.example/?w=52001234")).toBeNull();
    expect(parseWagonQr("javascript:alert(1)")).toBeNull();
    expect(parseWagonQr("DS-WAGON:123")).toBeNull();
    expect(parseWagonQr("")).toBeNull();
  });
});
