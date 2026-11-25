import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { type Cf2Doc, type LineKind, toSvgPath } from "../lib/cf2";

export const KIND_LABELS: Record<LineKind, string> = {
  cut: "Cut",
  crease: "Crease",
  perforation: "Perforation",
  score: "Score",
  other: "Other",
};

interface ViewBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

interface Props {
  doc: Cf2Doc;
  visible: Record<LineKind, boolean>;
  fitToken: number; // bump to re-fit
  onCursor: (pt: { x: number; y: number } | null) => void;
  onZoom: (pxPerMm: number) => void;
}

function fitBox(doc: Cf2Doc): ViewBox {
  const { xmin, ymin, xmax, ymax } = doc.bounds;
  const w = Math.max(xmax - xmin, 1);
  const h = Math.max(ymax - ymin, 1);
  const pad = Math.max(w, h) * 0.06;
  // y is negated when drawing, so the top of the model is at -ymax.
  return { x: xmin - pad, y: -ymax - pad, w: w + 2 * pad, h: h + 2 * pad };
}

export function DielineCanvas({ doc, visible, fitToken, onCursor, onZoom }: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [view, setView] = useState<ViewBox>(() => fitBox(doc));
  const drag = useRef<{ x: number; y: number } | null>(null);
  const fitRef = useRef<ViewBox>(fitBox(doc));
  fitRef.current = useMemo(() => fitBox(doc), [doc]);
  const endDrag = () => (drag.current = null);

  useEffect(() => setView(fitBox(doc)), [doc, fitToken]);

  const paths = useMemo(
    () => doc.primitives.map((p, i) => ({ key: i, type: p.type, d: toSvgPath(p) })),
    [doc],
  );

  // Pixels per SVG unit (mm) under preserveAspectRatio="xMidYMid meet".
  const scale = useCallback(() => {
    const ctm = svgRef.current?.getScreenCTM();
    return ctm ? ctm.a : 1;
  }, []);

  useEffect(() => {
    // Report after the browser has applied the new viewBox.
    const id = requestAnimationFrame(() => onZoom(scale()));
    return () => cancelAnimationFrame(id);
  }, [view, onZoom, scale]);

  // The scale also changes when the pane is resized (splitter, window).
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const ro = new ResizeObserver(() => onZoom(scale()));
    ro.observe(svg);
    return () => ro.disconnect();
  }, [onZoom, scale]);

  const toModel = (clientX: number, clientY: number) => {
    const svg = svgRef.current;
    const ctm = svg?.getScreenCTM();
    if (!svg || !ctm) return null;
    const p = new DOMPoint(clientX, clientY).matrixTransform(ctm.inverse());
    return { x: p.x, y: -p.y };
  };

  // Wheel zoom around the cursor. Registered natively so preventDefault works.
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const ctm = svg.getScreenCTM();
      if (!ctm) return;
      const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(ctm.inverse());
      const delta = Math.max(-200, Math.min(200, e.deltaY)); // one notch at most, however fast the wheel
      setView((v) => {
        // Keep zoom between 1/1000 and 20x of the fitted view so the viewBox stays finite.
        const fit = fitRef.current;
        const f = Math.min(Math.max(Math.exp(delta * 0.0015), (fit.w * 1e-3) / v.w), (fit.w * 20) / v.w);
        return { x: p.x - (p.x - v.x) * f, y: p.y - (p.y - v.y) * f, w: v.w * f, h: v.h * f };
      });
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, []);

  return (
    <svg
      ref={svgRef}
      className="canvas"
      viewBox={`${view.x} ${view.y} ${view.w} ${view.h}`}
      preserveAspectRatio="xMidYMid meet"
      onPointerDown={(e) => {
        if (e.button !== 0) return; // pan with the primary button only
        drag.current = { x: e.clientX, y: e.clientY };
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={(e) => {
        onCursor(toModel(e.clientX, e.clientY));
        if (!drag.current) return;
        const s = scale();
        if (!Number.isFinite(s) || s <= 0) return;
        const dx = (e.clientX - drag.current.x) / s;
        const dy = (e.clientY - drag.current.y) / s;
        drag.current = { x: e.clientX, y: e.clientY };
        setView((v) => ({ ...v, x: v.x - dx, y: v.y - dy }));
      }}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onLostPointerCapture={endDrag}
      onPointerLeave={() => onCursor(null)}
      onDoubleClick={() => setView(fitBox(doc))}
    >
      {paths.map(
        (p) => visible[p.type] && <path key={p.key} d={p.d} className={`ln ln-${p.type}`} />,
      )}
    </svg>
  );
}
