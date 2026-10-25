# vinyl-check brand

Wordmark, signet and logo (signet plus wordmark), each in anthracite for light backgrounds (`….svg`) and in light
for dark backgrounds (`…-inverse.svg`). They are drawn on the same grid as the pictograms in `../icons.svg`:
monoline, stroke 3 on a height of 24 units, square ends, only lines, circles and arcs. The signet is a record with a
tonearm. The red dot is the record label and the only colour (`--accent`).

| File | viewBox | Use |
|---|---|---|
| `vinyl-check-logo.svg`, `vinyl-check-logo-inverse.svg` | 0 0 204 32 | header, documents, README |
| `vinyl-check-wordmark.svg`, `vinyl-check-wordmark-inverse.svg` | 0 0 162 32 | wordmark alone, where the signet is already shown |
| `vinyl-check-signet.svg`, `vinyl-check-signet-inverse.svg` | 0 0 32 32 | favicon, app icon, small areas (from 16 px) |

| Variant | Strokes | Red dot |
|---|---|---|
| anthracite (light backgrounds) | `#1a1b1c` | `#b7382c` |
| inverted (dark backgrounds) | `#e9eaeb` | `#e05a48` |

In a Markdown page that should follow the reader's colour scheme, use a `<picture>` element:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="vinylcheck/static/brand/vinyl-check-logo-inverse.svg">
  <img alt="vinyl-check" src="vinylcheck/static/brand/vinyl-check-logo.svg" width="360">
</picture>
```

**Rules**

- Clear space: the height of the signet (32 units) on all sides.
- Minimum height: 20 px for the logo, 16 px for the signet.
- Do not distort, and do not recolour beyond anthracite or light and the red dot. No shadows or gradients.
- The name is always written in lowercase with a hyphen: **vinyl-check**.

The design system behind the brand is described in [docs/design-system.md](../../../docs/design-system.md).
