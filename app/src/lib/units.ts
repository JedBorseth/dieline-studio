// Inch formatting for R&L's conventions (ArtiosCAD shows fractions to 1/16).

export const MM_PER_INCH = 25.4;

/** Inches as a mixed fraction when exact to 1/32 (e.g. "14 5/16"), else up to 4 decimals. */
export function fmtIn(inches: number): string {
  const n = Math.round(inches * 32);
  if (Math.abs(inches * 32 - n) > 1e-6) return String(Number(inches.toFixed(4)));
  const sign = n < 0 ? "-" : "";
  const abs = Math.abs(n);
  const whole = Math.floor(abs / 32);
  let num = abs % 32;
  let den = 32;
  if (num === 0) return `${sign}${whole}`;
  while (num % 2 === 0) {
    num /= 2;
    den /= 2;
  }
  return whole ? `${sign}${whole} ${num}/${den}` : `${sign}${num}/${den}`;
}

export function mmToIn(mm: number): number {
  return mm / MM_PER_INCH;
}

/** Snaps 6-decimal CF2 float noise onto 1/32" multiples without distorting metric sizes. */
export function snapInchNoise(inches: number): number {
  const nearest32 = Math.round(inches * 32) / 32;
  return Math.abs(inches - nearest32) <= 1e-5 ? nearest32 : inches;
}

/** Formats inches as a fraction when exact, otherwise preserves the full decimal precision. */
export function fmtExactIn(inches: number): string {
  const formatted = fmtIn(inches);
  return parseLength(formatted) === inches ? formatted : String(inches);
}

/** Converts a typed length string when toggling form units, preserving 1/32" fractions cleanly. */
export function convertLengthText(text: string, toUnits: "in" | "mm"): string {
  const v = parseLength(text);
  if (!Number.isFinite(v)) return text;
  if (toUnits === "mm") return String(Number((v * MM_PER_INCH).toFixed(4)));
  const inches = v / MM_PER_INCH;
  const nearest32 = Math.round(inches * 32) / 32;
  if (Math.abs(inches - nearest32) <= 1e-4) return fmtIn(nearest32);
  return fmtIn(Number(inches.toFixed(4)));
}

const MIXED = /^\s*(?:(\d+(?:\.\d+)?)\s*[-+ ]\s*)?(\d+)\s*\/\s*(\d+)\s*$/;
// ArtiosCAD's sixteenths notation (shared default SixteenthChar ":"): 10:03 = 10 3/16.
const SIXTEENTHS = /^(\d+)\s*:\s*(\d{1,2})$/;

/**
 * Parses "14", "14.125", "14 1/8", "14-1/8", "14+1/8" (ArtiosCAD display), "14:02" (ArtiosCAD
 * sixteenths, = 14 2/16), "7/16", "-1/2" (optional trailing "). NaN if not a length.
 */
export function parseLength(text: string): number {
  const t = text.trim().replace(/"$/, "").trim();
  if (/^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(t)) return Number(t);
  const unsigned = t.replace(/^[+-]/, "");
  const sx = SIXTEENTHS.exec(unsigned);
  if (sx) {
    const sixteenths = Number(sx[2]);
    if (sixteenths >= 16) return Number.NaN;
    const v = Number(sx[1]) + sixteenths / 16;
    return t.startsWith("-") ? -v : v;
  }
  const m = MIXED.exec(unsigned);
  if (!m) return Number.NaN;
  const den = Number(m[3]);
  if (den === 0) return Number.NaN;
  const value = Number(m[1] ?? 0) + Number(m[2]) / den;
  return t.startsWith("-") ? -value : value;
}
