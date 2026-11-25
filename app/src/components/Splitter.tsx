import { useRef } from "react";

/** Vertical drag handle; reports horizontal movement in px. */
export function Splitter({ onDrag }: { onDrag: (dx: number) => void }) {
  const last = useRef<number | null>(null);
  return (
    <div
      className="group relative z-10 -mx-[3px] w-[7px] shrink-0 cursor-col-resize touch-none"
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
    >
      <div className="pointer-events-none absolute inset-y-0 left-[3px] w-px bg-border transition-[background-color,width,left] group-hover:left-[2px] group-hover:w-[3px] group-hover:bg-ring group-active:left-[2px] group-active:w-[3px] group-active:bg-ring" />
    </div>
  );
}
