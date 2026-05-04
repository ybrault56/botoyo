---
name: Premium AI Design System
colors:
  surface: '#051424'
  surface-dim: '#051424'
  surface-bright: '#2c3a4c'
  surface-container-lowest: '#010f1f'
  surface-container-low: '#0d1c2d'
  surface-container: '#122131'
  surface-container-high: '#1c2b3c'
  surface-container-highest: '#273647'
  on-surface: '#d4e4fa'
  on-surface-variant: '#c2c7d2'
  inverse-surface: '#d4e4fa'
  inverse-on-surface: '#233143'
  outline: '#8c919c'
  outline-variant: '#424750'
  surface-tint: '#a3c9ff'
  primary: '#b2d1ff'
  on-primary: '#00315c'
  primary-container: '#7eb6ff'
  on-primary-container: '#004680'
  inverse-primary: '#1c60a3'
  secondary: '#bfc7d7'
  on-secondary: '#29313e'
  secondary-container: '#3f4755'
  on-secondary-container: '#adb5c6'
  tertiary: '#cacfd9'
  on-tertiary: '#2c3139'
  tertiary-container: '#afb3bd'
  on-tertiary-container: '#41454e'
  error: '#ffb4ab'
  on-error: '#690005'
  error-container: '#93000a'
  on-error-container: '#ffdad6'
  primary-fixed: '#d3e3ff'
  primary-fixed-dim: '#a3c9ff'
  on-primary-fixed: '#001c39'
  on-primary-fixed-variant: '#004882'
  secondary-fixed: '#dbe3f4'
  secondary-fixed-dim: '#bfc7d7'
  on-secondary-fixed: '#141c28'
  on-secondary-fixed-variant: '#3f4755'
  tertiary-fixed: '#dee2ed'
  tertiary-fixed-dim: '#c2c6d1'
  on-tertiary-fixed: '#171c23'
  on-tertiary-fixed-variant: '#42474f'
  background: '#051424'
  on-background: '#d4e4fa'
  surface-variant: '#273647'
typography:
  display-xl:
    fontFamily: Newsreader
    fontSize: 84px
    fontWeight: '400'
    lineHeight: '1.1'
    letterSpacing: -0.02em
  display-lg:
    fontFamily: Newsreader
    fontSize: 64px
    fontWeight: '400'
    lineHeight: '1.1'
    letterSpacing: -0.01em
  headline-md:
    fontFamily: Newsreader
    fontSize: 32px
    fontWeight: '400'
    lineHeight: '1.2'
  body-lg:
    fontFamily: Inter
    fontSize: 18px
    fontWeight: '400'
    lineHeight: '1.6'
  body-md:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '400'
    lineHeight: '1.5'
  label-caps:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '600'
    lineHeight: '1'
    letterSpacing: 0.1em
  nav-link:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '500'
    lineHeight: '1'
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  unit: 4px
  gutter: 24px
  margin-page: 64px
  container-max: 1280px
---

## Brand & Style
This design system embodies a premium, AI-centric aesthetic that balances technical precision with editorial sophistication. The visual narrative is driven by **Glassmorphism** and **Modern Minimalism**, utilizing deep tonal depth and luminous accents to evoke a sense of high-end intelligence. 

The strategy relies on a "Dark Mode First" approach, where the interface feels like a sophisticated digital cockpit. High-contrast serif typography provides a humanistic, authoritative touch, while glowing nodes and thin-stroke borders reinforce the cutting-edge technological nature of the product.

## Colors
The palette is rooted in a deep midnight foundation to ensure maximum contrast for glowing elements.

- **Primary**: A vibrant, luminous blue used exclusively for primary actions, active states, and focus indicators.
- **Backgrounds**: A tiered system of near-blacks and deep navy blues. The base layer is almost black, while interactive surfaces use slightly lighter navy tones to create depth.
- **Accents**: Subtle grays and low-opacity whites are used for borders and secondary metadata to maintain a clean, "quiet" interface.
- **Glows**: Soft radial gradients using the primary blue at 10-20% opacity are used to highlight AI-driven components and central focal points.

## Typography
The typographic system creates a tension between the traditional and the futuristic. 

- **Headlines**: Use **Newsreader** for a sophisticated, editorial feel. Italic variants should be used selectively for emphasis to add a sense of motion and craftsmanship.
- **Body & Interface**: **Inter** provides high legibility and a neutral, systematic feel for functional text. 
- **Labels**: Small-scale labels should utilize increased letter-spacing and uppercase styling to denote "technical" or "system" metadata, typical of AI data visualizations.

## Layout & Spacing
The layout uses a **Fluid Grid** system with generous whitespace to maintain a premium feel. 

- **Grid**: A 12-column grid is standard for web views, but content is often intentionally offset to create a dynamic, non-linear flow.
- **Rhythm**: A 4px baseline grid ensures vertical consistency.
- **Background Pattern**: A subtle, dark grid overlay (approx. 40px squares) is used in the background to provide a sense of mathematical scale and structure.

## Elevation & Depth
Depth is achieved through **translucency** rather than traditional shadows.

- **Glassmorphism**: Cards and floating panels use a semi-transparent background (`rgba(18, 26, 38, 0.7)`) with a high-saturation `backdrop-filter: blur(12px)`.
- **Thin Borders**: Elements are defined by 1px solid borders with low opacity (`rgba(255, 255, 255, 0.1)`). This creates a "blueprint" or "HUD" effect.
- **Luminous Nodes**: Important interactive nodes use a "halo" effect—a concentrated inner glow combined with a soft outer radial blur to simulate a light source.

## Shapes
The shape language is consistently **Rounded**, striking a balance between organic and geometric. 

- **Standard Containers**: Use a 0.5rem (8px) radius for a modern feel.
- **Interactive Elements**: Buttons and primary chips use a pill-shape (full rounding) to differentiate them from structural content.
- **Connectors**: Data visualization lines use extremely thin weights (1px) with rounded caps and circular nodes at intersection points.

## Components
Consistent application of glassmorphism and the primary blue accent defines the component library.

- **Primary Button**: Pill-shaped with a solid primary blue fill and dark text. Includes a subtle arrow icon for directional momentum.
- **Secondary Button**: Pill-shaped with a transparent background and a 1px border. Text is white for high visibility.
- **Status Chips**: Small, dark-filled capsules with a 1px border and a leading dot icon (glowing) to indicate system status or categories.
- **Data Cards**: Glassmorphic surfaces with a vertical internal hierarchy. Top sections usually contain a small icon in a square container with a thin border.
- **Navigation**: Minimalist text-only links in the header, with a primary CTA often isolated on the far right to drive conversion.
- **Visual Nodes**: Centralized AI "cores" are rendered as glowing orbs with orbital rings, serving as both a visual anchor and a state indicator.