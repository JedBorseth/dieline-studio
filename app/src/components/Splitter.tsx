import { useRef } from "react";

const KEY_STEP = 24;

interface Props {
  /** Reports horizontal movement in px (positive = rightwards). */
  onDrag: (dx: number) => void;
  /** Current preview width, for assistive technology. */
  value: number;
  min: number;
  max: number;
}

/** Vertical drag handle between the chat and the preview. Arrow keys resize too. */
export function Splitter({ onDrag, value, min, max }: Props) {
  const last = useRef<number | null>(null);
  const end = () => (last.current = null);
  return (
    <div
      className="group relative z-10 -mx-[3px] w-[7px] shrink-0 cursor-col-resize touch-none outline-none focus-visible:[&>div]:left-[2px] focus-visible:[&>div]:w-[3px] focus-visible:[&>div]:bg-ring"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize preview pane"
      aria-valuenow={Math.round(value)}
      aria-valuemin={Math.round(min)}
      aria-valuemax={Math.round(max)}
      tabIndex={0}
      onPointerDown={(e) => {
        if (e.button !== 0) return;
        last.current = e.clientX;
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={(e) => {
        if (last.current === null) return;
        onDrag(e.clientX - last.current);
        last.current = e.clientX;
      }}
      onPointerUp={end}
      onPointerCancel={end}
      onLostPointerCapture={end}
      onKeyDown={(e) => {
        // The preview is on the right: moving the handle left widens it.
        if (e.key === "ArrowLeft") onDrag(-KEY_STEP);
        else if (e.key === "ArrowRight") onDrag(KEY_STEP);
        else return;
        e.preventDefault();
      }}
    >
      <div className="pointer-events-none absolute inset-y-0 left-[3px] w-px bg-border transition-[background-color,width,left] group-hover:left-[2px] group-hover:w-[3px] group-hover:bg-ring group-active:left-[2px] group-active:w-[3px] group-active:bg-ring" />
    </div>
  );
}
