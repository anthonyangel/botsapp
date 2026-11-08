# 0032 — Admin UI moves from Material Design Lite to Web Awesome

**Status:** Decided and implemented (2026-11). Replaces the (never recorded) choice of Material Design Lite 1.3.0 for the admin UI.

## Context

The admin UI's styling came from Material Design Lite (MDL), loaded from cdnjs, with no decision record behind it: it was picked for convenience (one `<link>`, one `<script>`, no build step, server-rendered f-strings). Two things made that choice stop working:

- **MDL is frozen and deprecated** (last release 2015-era 1.3.0). It has no responsive scaffolding beyond the grid: on a phone the header nav overflowed, the tables only scrolled sideways, and `_EXTRA_CSS` had grown into a large pile of overrides (chips, tables, text fields, light-only colour forcing) that mostly re-implemented what a current library ships.
- **The admin UI is now a real product surface** (public under `/admin`, 0028; accounts, grants, keys, users), and is used from phones.

## Decision

- **Web Awesome 3 (`@awesome.me/webawesome`, MIT, pinned to an exact version) replaces MDL.** It is loaded from its CDN as ES modules (`webawesome.css` + `webawesome.loader.js`), so there is still **no build step and no JS framework** — pages remain server-rendered Python f-strings, with `html.escape` on every WhatsApp-originated value as before.
- **Layout is `<wa-page>`**: the nav is written once in `slot="navigation"` and the component renders it as a desktop sidebar or a mobile drawer. Forms and cards use `wa-stack`/`wa-cluster`/`wa-flank` layout utilities and `--wa-*` tokens; no hex colours or pixel literals in the page CSS, so theming and dark mode are available through tokens.
- **Components replace hand-rolled controls**: `wa-switch` (allowlist toggle), `wa-tag` (tags, with `with-remove`), `wa-input`, `wa-button`, `wa-tab-group`, `wa-card`, `wa-callout`, `wa-badge`, `wa-avatar`.
- **Tables stay `<table>`.** Web Awesome has no responsive table, so below 640px each row is restyled as a card (`data-label` on cells supplies the column names). Column sorting is desktop-only because the header row is hidden on phones.
- **Behaviour is unchanged.** Same routes, same form fields (`is_allowed`, `tags`, …), same background-save pattern; only the markup, CSS and the client-side glue (tag chips, filter chips, tab restore) were rewritten for the new components.
- **Web Awesome's agent skills** (`webawesome`, `webawesome-design`, from the npm package) are a developer-side aid, installed per user. They are not a dependency and nothing from them ships.

## Alternatives considered

| Option | Why not |
|---|---|
| Stay on MDL and patch (drawer, table CSS) | Cheapest, but keeps a frozen 2015 library and a growing override pile. |
| Hand-written stylesheet, no library | Viable for ~5 pages, but means owning every control's states, focus and accessibility. |
| Material Web (`@material/web`, M3) | Closest to the old look, but it is in maintenance mode pending new maintainers, and has no page shell or table, so the mobile layout would have been hand-written anyway. |
| Pico.css / Tailwind | Classless/utility CSS gives no components (switch, tabs, drawer). |
| A JS framework (React/Vue/Svelte) | Needs a Node build, a JSON API in front of every page and a second deploy artifact, for about five pages. |

## Consequences

- **Third-party script from a CDN on the admin origin, as before.** MDL came from cdnjs with no integrity check; Web Awesome comes from `ka-f.webawesome.com`. The version is pinned, and Subresource Integrity is set on the loader script. It is not set on the stylesheet (the CDN sends no CORS headers for it, so a stylesheet with `integrity=` is blocked outright — found when first rendering the pages), and it does not cover the chunks the loader imports at runtime, so it is partial protection. Self-hosting the `dist-cdn` files (e.g. in the image, served by Caddy) would close this; left as a follow-up, not done here.
- **Light theme only for now** (`wa-light`). MDL's theme was forced light; Web Awesome supports `wa-dark`, so following the OS preference is a small follow-up.
- **Custom-element events replace MDL's DOM classes**: tag removal listens for `wa-remove` (delegated, since tag nodes are re-rendered), the allow switch uses `change`. Tests assert on form field names, not component markup, so they are unaffected.
- **Major-version risk:** Web Awesome 3 is recent and already deprecates some long-form values (`size="small"` → `"s"`). The version is pinned; bumping it means re-checking those warnings.
