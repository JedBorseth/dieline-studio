import type { ReactElement, ReactNode } from "react";

import { Tooltip, TooltipPopup, TooltipTrigger } from "./ui/tooltip";

/** Wraps a single element (usually a Button) with a hover tooltip. */
export function Tip({
  label,
  side,
  children,
}: {
  label: ReactNode;
  side?: "top" | "bottom" | "left" | "right";
  children: ReactElement;
}) {
  return (
    <Tooltip>
      <TooltipTrigger render={children} />
      <TooltipPopup side={side}>{label}</TooltipPopup>
    </Tooltip>
  );
}
