// Adapted from T3 Code (MIT), apps/web/src/lib/utils.ts (the `cn` helper only). See THIRD_PARTY_NOTICES.md.
import { type CxOptions, cx } from "class-variance-authority";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: CxOptions) {
  return twMerge(cx(inputs));
}
