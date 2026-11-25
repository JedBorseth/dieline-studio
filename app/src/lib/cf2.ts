// CF2 parser for the preview. Mirrors engine/src/dieline_engine/cf2.py's
// reader but keeps the raw line-type code, so files exported from ArtiosCAD
// (whose codes may differ from ours) still render and can be inspected.
// Output coordinates are millimetres, y up.

export type LineKind = "cut" | "crease" | "perforation" | "score" | "other";

// UNCONFIRMED mapping, same as the engine's CF2_LINE_CODES.
export const CODE_KINDS: Record<number, LineKind> = { 1: "cut", 2: "crease", 3: "perforation", 4: "score" };

export type Primitive =
  | { kind: "line"; code: number; type: LineKind; x1: number; y1: number; x2: number; y2: number }
  | {
      kind: "arc";
      code: number;
      type: LineKind;
      sx: number;
      sy: number;
      ex: number;
      ey: number;
      cx: number;
      cy: number;
      cw: boolean;
    };

export interface Bounds {
  xmin: number;
  ymin: number;
  xmax: number;
  ymax: number;
}

export interface Cf2Doc {
  name: string;
  units: "mm" | "in";
  primitives: Primitive[];
  bounds: Bounds;
  codes: Record<number, number>;
  warnings: string[];
}

/** x' = a*x + b*y + e,  y' = c*x + d*y + f */
interface Affine {
  a: number;
  b: number;
  c: number;
  d: number;
  e: number;
  f: number;
}

const IDENTITY: Affine = { a: 1, b: 0, c: 0, d: 1, e: 0, f: 0 };

/** CFF2 subroutine call: rotate, then scale/mirror, then translate. */
function callTransform(dx: number, dy: number, angleDeg: number, sx: number, sy: number): Affine {
  const r = (angleDeg * Math.PI) / 180;
  const cos = Math.cos(r);
  const sin = Math.sin(r);
  return { a: sx * cos, b: -sx * sin, c: sy * sin, d: sy * cos, e: dx, f: dy };
}

function apply(t: Affine, x: number, y: number): [number, number] {
  return [t.a * x + t.b * y + t.e, t.c * x + t.d * y + t.f];
}

/** The transform that applies `inner` first, then `outer`. */
function compose(inner: Affine, outer: Affine): Affine {
  const o = outer;
  return {
    a: o.a * inner.a + o.b * inner.c,
    b: o.a * inner.b + o.b * inner.d,
    c: o.c * inner.a + o.d * inner.c,
    d: o.c * inner.b + o.d * inner.d,
    e: o.a * inner.e + o.b * inner.f + o.e,
    f: o.c * inner.e + o.d * inner.f + o.f,
  };
}

function isConformal(t: Affine): boolean {
  const n1 = Math.hypot(t.a, t.c);
  const n2 = Math.hypot(t.b, t.d);
  const dot = t.a * t.b + t.c * t.d;
  return Math.abs(n1 - n2) <= 1e-9 * Math.max(n1, n2, 1) && Math.abs(dot) <= 1e-9 * Math.max(n1 * n2, 1);
}

export function parseCf2(text: string, maxDepth = 16): Cf2Doc {
  const blocks = new Map<string, string[][]>();
  let main: string | null = null;
  let current: string[][] | null = null;
  let units: "mm" | "in" = "mm";
  const warnings: string[] = [];

  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    const f = line.split(",").map((s) => s.trim());
    const key = f[0].toUpperCase();
    if ((key === "MAIN" || key === "SUB") && f.length > 1) {
      current = blocks.get(f[1]) ?? [];
      blocks.set(f[1], current);
      if (key === "MAIN") main = f[1];
    } else if (key === "END") {
      current = null;
    } else if (current) {
      if (key === "UM") units = "mm";
      else if (key === "UI") units = "in";
      else current.push(f);
    }
  }
  if (main === null) throw new Error("Not a CF2 file: no MAIN block");

  const scale = units === "in" ? 25.4 : 1;
  const primitives: Primitive[] = [];
  const codes: Record<number, number> = {};
  const kindOf = (s: string): [number, LineKind] => {
    const code = Math.trunc(Number(s));
    codes[code] = (codes[code] ?? 0) + 1;
    return [code, CODE_KINDS[code] ?? "other"];
  };
  const num = (s: string) => {
    const v = Number(s);
    if (!Number.isFinite(v)) throw new Error(`bad number ${s}`);
    return v;
  };

  const emit = (name: string, t: Affine, depth: number) => {
    if (depth > maxDepth) {
      warnings.push(`subroutine nesting deeper than ${maxDepth} at "${name}"`);
      return;
    }
    const mirrored = t.a * t.d - t.b * t.c < 0;
    const conformal = isConformal(t);
    for (const f of blocks.get(name) ?? []) {
      const key = f[0].toUpperCase();
      try {
        if (key === "L" && f.length >= 8) {
          const [code, type] = kindOf(f[2]);
          const [x1, y1] = apply(t, num(f[4]) * scale, num(f[5]) * scale);
          const [x2, y2] = apply(t, num(f[6]) * scale, num(f[7]) * scale);
          primitives.push({ kind: "line", code, type, x1, y1, x2, y2 });
        } else if (key === "A" && f.length >= 11) {
          const [code, type] = kindOf(f[2]);
          const [sx, sy] = apply(t, num(f[4]) * scale, num(f[5]) * scale);
          const [ex, ey] = apply(t, num(f[6]) * scale, num(f[7]) * scale);
          const [cx, cy] = apply(t, num(f[8]) * scale, num(f[9]) * scale);
          let cw = Math.trunc(num(f[10])) === -1;
          if (mirrored) cw = !cw;
          if (!conformal) warnings.push(`arc in "${name}" is scaled non-uniformly; drawn as a circular arc`);
          primitives.push({ kind: "arc", code, type, sx, sy, ex, ey, cx, cy, cw });
        } else if (key === "C" && f.length >= 3) {
          if (!blocks.has(f[1])) {
            warnings.push(`call to undefined subroutine "${f[1]}"`);
            continue;
          }
          const n = [...f.slice(2, 7).map(num), ...[0, 0, 0, 1, 1].slice(f.slice(2, 7).length)];
          const inner = callTransform(n[0] * scale, n[1] * scale, n[2], n[3], n[4]);
          emit(f[1], compose(inner, t), depth + 1);
        }
      } catch {
        warnings.push(`skipped unparseable record: ${f.join(",")}`);
      }
    }
  };
  emit(main, IDENTITY, 0);

  return { name: main, units, primitives, bounds: boundsOf(primitives), codes, warnings };
}

function boundsOf(prims: Primitive[]): Bounds {
  let xmin = Infinity;
  let ymin = Infinity;
  let xmax = -Infinity;
  let ymax = -Infinity;
  const add = (x: number, y: number) => {
    xmin = Math.min(xmin, x);
    ymin = Math.min(ymin, y);
    xmax = Math.max(xmax, x);
    ymax = Math.max(ymax, y);
  };
  for (const p of prims) {
    if (p.kind === "line") {
      add(p.x1, p.y1);
      add(p.x2, p.y2);
    } else {
      // Conservative: the arc's full circle box.
      const r = Math.hypot(p.sx - p.cx, p.sy - p.cy);
      add(p.sx, p.sy);
      add(p.ex, p.ey);
      const a0 = Math.atan2(p.sy - p.cy, p.sx - p.cx);
      const sweep = arcSweep(p);
      for (let k = -8; k <= 8; k++) {
        const ang = (k * Math.PI) / 2;
        const u = (ang - a0) / sweep;
        if (u > 0 && u < 1) add(p.cx + r * Math.cos(ang), p.cy + r * Math.sin(ang));
      }
    }
  }
  if (!Number.isFinite(xmin)) return { xmin: 0, ymin: 0, xmax: 0, ymax: 0 };
  return { xmin, ymin, xmax, ymax };
}

/** Signed sweep in radians, positive = counter-clockwise. A closed arc sweeps a full turn. */
export function arcSweep(p: Extract<Primitive, { kind: "arc" }>): number {
  const a0 = Math.atan2(p.sy - p.cy, p.sx - p.cx);
  const a1 = Math.atan2(p.ey - p.cy, p.ex - p.cx);
  let s = a1 - a0;
  if (p.cw) while (s >= 0) s -= 2 * Math.PI;
  else while (s <= 0) s += 2 * Math.PI;
  return s;
}

/** SVG path for a primitive, with y negated so the drawing reads y-up. */
export function toSvgPath(p: Primitive): string {
  if (p.kind === "line") return `M${p.x1} ${-p.y1}L${p.x2} ${-p.y2}`;
  const r = Math.hypot(p.sx - p.cx, p.sy - p.cy);
  const sweep = arcSweep(p);
  // Negating y flips the angle sign: model CCW becomes SVG's negative-angle direction (sweep-flag 0).
  const flag = sweep > 0 ? 0 : 1;
  if (Math.abs(Math.abs(sweep) - 2 * Math.PI) < 1e-9) {
    // Full circle: SVG can't draw it in one arc; go via the opposite point.
    const ox = 2 * p.cx - p.sx;
    const oy = 2 * p.cy - p.sy;
    return `M${p.sx} ${-p.sy}A${r} ${r} 0 0 ${flag} ${ox} ${-oy}A${r} ${r} 0 0 ${flag} ${p.sx} ${-p.sy}`;
  }
  const large = Math.abs(sweep) > Math.PI ? 1 : 0;
  return `M${p.sx} ${-p.sy}A${r} ${r} 0 ${large} ${flag} ${p.ex} ${-p.ey}`;
}
