# Third-party notices

## Code adapted from T3 Code

Some UI primitives in this app are adapted from [T3 Code](https://github.com/pingdotgg/t3code)
(MIT License). Each adapted file starts with a header comment naming its upstream path.
Changes from upstream: imports rewritten to relative paths; i18n (`gt-react`) replaced with
plain English strings; Electron titlebar handling (`useNativeTitlebarHeight`) and the
visible-animation observer removed; colours come from this app's own theme tokens in
`src/styles.css`.

| File in this repo | Upstream file |
| --- | --- |
| `src/components/ui/alert.tsx` | `apps/web/src/components/ui/alert.tsx` |
| `src/components/ui/badge.tsx` | `apps/web/src/components/ui/badge.tsx` |
| `src/components/ui/button.tsx` | `apps/web/src/components/ui/button.tsx` |
| `src/components/ui/checkbox.tsx` | `apps/web/src/components/ui/checkbox.tsx` |
| `src/components/ui/collapsible.tsx` | `apps/web/src/components/ui/collapsible.tsx` |
| `src/components/ui/dialog.tsx` | `apps/web/src/components/ui/dialog.tsx` |
| `src/components/ui/dialog-styles.ts` | `apps/web/src/components/ui/dialog-styles.ts` |
| `src/components/ui/input.tsx` | `apps/web/src/components/ui/input.tsx` |
| `src/components/ui/label.tsx` | `apps/web/src/components/ui/label.tsx` |
| `src/components/ui/scroll-area.tsx` | `apps/web/src/components/ui/scroll-area.tsx` |
| `src/components/ui/select.tsx` | `apps/web/src/components/ui/select.tsx` |
| `src/components/ui/spinner.tsx` | `apps/web/src/components/ui/spinner.tsx` |
| `src/components/ui/textarea.tsx` | `apps/web/src/components/ui/textarea.tsx` |
| `src/components/ui/tooltip.tsx` | `apps/web/src/components/ui/tooltip.tsx` |
| `src/lib/utils.ts` (`cn` only) | `apps/web/src/lib/utils.ts` |

```
MIT License

Copyright (c) 2026 T3 Tools Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## npm dependencies added with the UI primitives

Runtime (bundled into the app):

| Package | Version | License |
| --- | --- | --- |
| `@base-ui/react` | 1.9.0 | MIT |
| `class-variance-authority` | 0.7.1 | Apache-2.0 |
| `clsx` | 2.1.1 | MIT |
| `tailwind-merge` | 3.7.1 | MIT |
| `lucide-react` | 1.79.0 | ISC |

Transitive runtime dependencies of `@base-ui/react`: `@base-ui/utils`, `@floating-ui/core`,
`@floating-ui/dom`, `@floating-ui/react-dom`, `@floating-ui/utils`, `@babel/runtime`,
`reselect`, `use-sync-external-store` (all MIT).

Build-time only (not shipped):

| Package | Version | License |
| --- | --- | --- |
| `tailwindcss` | 4.3.5 | MIT |
| `@tailwindcss/vite` | 4.3.5 | MIT |

Tailwind's build pulls in `@tailwindcss/node`/`oxide`, `enhanced-resolve`, `jiti`,
`magic-string`, `tapable` (MIT), `graceful-fs` (BlueOak-1.0.0, permissive) and
`lightningcss` (MPL-2.0, already a dependency of Vite). None of these are bundled into the app.
