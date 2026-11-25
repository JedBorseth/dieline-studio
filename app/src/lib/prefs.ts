// Per-machine UI conveniences (pane widths, toggles). Storage may be
// unavailable; every access is guarded and has a default.

const PREFIX = "dieline-studio:";

export function loadNumber(key: string, fallback: number): number {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    const n = raw === null ? NaN : Number(raw);
    return Number.isFinite(n) ? n : fallback;
  } catch {
    return fallback;
  }
}

export function saveNumber(key: string, value: number): void {
  try {
    localStorage.setItem(PREFIX + key, String(Math.round(value)));
  } catch {
    // ignore
  }
}

export function loadString(key: string, fallback: string): string {
  try {
    return localStorage.getItem(PREFIX + key) ?? fallback;
  } catch {
    return fallback;
  }
}

export function saveString(key: string, value: string): void {
  try {
    localStorage.setItem(PREFIX + key, value);
  } catch {
    // ignore
  }
}
