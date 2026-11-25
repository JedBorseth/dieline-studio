import { useRef } from "react";

/** Vertical drag handle; reports horizontal movement in px. */
export function Splitter({ onDrag }: { onDrag: (dx: number) => void }) {
  const last = useRef<number | null>(null);
  return (
    <div
      className="splitter"
      role="separator"
      aria-orientation="vertical"
      onPointerDown={(e) => {
        last.current = e.clientX;
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={(e) => {
        if (last.current === null) return;
        onDrag(e.clientX - last.current);
        last.current = e.clientX;
      }}
      onPointerUp={() => (last.current = null)}
      onPointerCancel={() => (last.current = null)}
    />
  );
}
