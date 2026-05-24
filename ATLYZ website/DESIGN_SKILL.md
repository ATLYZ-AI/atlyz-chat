---
name: atlyz-website-redesign
description: Design skill for upgrading the Atlyz website to premium agency-tier quality. Pulls from taste-skill, redesign-skill, and soft-skill principles. Tailored for vanilla HTML/CSS (no React/Tailwind). Fixes AI-generic patterns found in current site.
---

# Atlyz Website Design Skill
> Based on: https://github.com/Leonxlnx/taste-skill
> Stack: Vanilla HTML + CSS + vanilla JS (no frameworks, no build step)

---

## 1. CURRENT SITE AUDIT — WHAT'S WRONG

Running the redesign-skill audit on the existing pages reveals these problems to fix:

### Banned patterns currently in use:
- **Inter font everywhere** — the #1 AI fingerprint. Must be replaced.
- **Purple/blue AI gradient aesthetic** (`--purple: #a78bfa`, `--blue: #60a5fa`) — the "Lila Ban" violation. Most common AI design tell.
- **Centered hero layout** — `text-align: center` on everything. Anti-center bias rule is violated.
- **3-column equal card grid** (features section) — the most generic AI layout, strictly banned.
- **Pure glows / neon outer shadows** — `box-shadow` glows on cards and buttons.
- **Circular spinners** and generic loading states.
- **Generic card pattern** (border + shadow + dark bg) used for every single feature.
- **AI copywriting** — "Seamless", "Next-Gen", "Unleash"-style language likely present.
- **Sticky nav glued to top edge** — should be floating pill instead.

---

## 2. ACTIVE DESIGN DIALS FOR ATLYZ

```
DESIGN_VARIANCE:  7   (asymmetric, modern — not artsy chaos, but not centered either)
MOTION_INTENSITY: 5   (smooth CSS transitions + scroll reveals, no complex physics needed)
VISUAL_DENSITY:   4   (SaaS landing page — breathable, but data is present)
```

These are fixed for the Atlyz site. Do not change unless the user explicitly asks.

---

## 3. ATLYZ BRAND TOKENS (Replacement Palette)

Replace the current purple-heavy palette with this:

```css
:root {
  /* Backgrounds */
  --bg:        #080810;      /* Deep OLED, not pure black */
  --surface:   #0f0f1a;
  --surface2:  #16162a;
  --surface3:  #1e1e30;     /* Card backgrounds */

  /* Borders */
  --border:    rgba(255,255,255,0.07);
  --border-h:  rgba(255,255,255,0.14);

  /* Text */
  --text:      #eeeef4;
  --muted:     #7070a0;
  --subtle:    #50507a;

  /* ONE accent — Electric Blue (not purple) */
  --accent:    #4f8ef7;      /* Single accent — use sparingly */
  --accent-dim: rgba(79,142,247,0.12);

  /* Success / data */
  --green:     #3ecf8e;      /* Supabase-style green for positive metrics */
  --mono:      'JetBrains Mono', monospace;  /* For stats/numbers only */
}
```

**Rules:**
- Max 1 accent color (`--accent`). Never use purple, violet, or pink.
- Never use `#000000`. Use `--bg` (#080810) as darkest.
- All grays must be cool-tinted (blue-gray family). No warm grays.
- Gradient text on headers: BANNED. Use solid `--text` or `--accent` only.

---

## 4. TYPOGRAPHY SYSTEM

Replace Inter with this pairing:

```html
<!-- In <head> of every page -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=Syne:wght@700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
```

```css
/* Typography scale */
body          { font-family: 'Outfit', sans-serif; font-size: 16px; line-height: 1.7; }
h1, h2        { font-family: 'Syne', sans-serif; letter-spacing: -0.03em; line-height: 1.1; }
.mono, .stat  { font-family: 'JetBrains Mono', monospace; font-variant-numeric: tabular-nums; }

/* Scale */
.display      { font-size: clamp(2.8rem, 6vw, 5.5rem); font-weight: 800; }
.h2           { font-size: clamp(2rem, 4vw, 3.2rem);   font-weight: 700; }
.h3           { font-size: 1.4rem;                      font-weight: 600; }
.body         { font-size: 1rem;   max-width: 65ch;     color: var(--muted); }
.eyebrow      { font-size: 0.7rem; letter-spacing: 0.18em; text-transform: uppercase; font-weight: 500; }
```

**Rules:**
- `Syne` for all display/H1/H2 headlines — gives character without screaming.
- `Outfit` for all body text — clean, modern, not Inter.
- `JetBrains Mono` ONLY for numbers, stats, code, pricing.
- Sentence case on all headers. No Title Case.
- `text-wrap: balance` on all H1/H2 to prevent orphaned words.

---

## 5. LAYOUT DIRECTIVES

### 5a. Navigation — Floating Pill

Replace the sticky edge-glued navbar with a floating glass pill:

```css
nav {
  position: fixed;
  top: 20px;
  left: 50%;
  transform: translateX(-50%);
  width: max-content;
  max-width: calc(100vw - 48px);
  border-radius: 100px;
  padding: 0 28px;
  height: 52px;
  background: rgba(15, 15, 26, 0.85);
  backdrop-filter: blur(24px);
  border: 1px solid var(--border);
  box-shadow: 0 0 0 1px rgba(255,255,255,0.04) inset;
}
```

### 5b. Hero — Asymmetric Split

Never center the hero. Use left-aligned text + right-side visual:

```css
.hero {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4rem;
  align-items: center;
  min-height: 100dvh;           /* NOT h-screen — avoids iOS Safari jump */
  padding: 120px 80px 80px;
}

/* Mobile */
@media (max-width: 768px) {
  .hero { grid-template-columns: 1fr; padding: 100px 24px 60px; }
  .hero-visual { display: none; }  /* Or stack below text */
}
```

### 5c. Features — Bento Grid (Not 3 equal cards)

Replace 3-column equal card grid with asymmetric Bento:

```css
.bento {
  display: grid;
  grid-template-columns: 2fr 1fr 1fr;
  grid-template-rows: auto auto;
  gap: 16px;
}
.bento-wide  { grid-column: span 2; }
.bento-tall  { grid-row: span 2; }

@media (max-width: 768px) {
  .bento, .bento-wide, .bento-tall {
    grid-column: 1; grid-row: auto;
    display: block;
  }
}
```

### 5d. Section Spacing

Minimum section padding: `120px` top/bottom on desktop, `64px` on mobile. Let the layout breathe.

---

## 6. CARD & COMPONENT RULES

### Premium Card (Double-Bezel technique)

```html
<div class="card-shell">    <!-- Outer shell: subtle bg + hairline border -->
  <div class="card-core">   <!-- Inner core: actual content -->
    ...
  </div>
</div>
```

```css
.card-shell {
  background: rgba(255,255,255,0.03);
  border: 1px solid var(--border);
  border-radius: 24px;
  padding: 3px;
}
.card-core {
  background: var(--surface3);
  border-radius: 21px;          /* calc(24px - 3px) */
  padding: 28px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.07);
}
```

**Card rules:**
- Cards ONLY when elevation communicates hierarchy. Flat feature lists → use `border-top` dividers, not cards.
- Shadow must be tinted to background: `box-shadow: 0 20px 60px rgba(15,15,40,0.6)` — not pure black.
- Border-radius: `24px` outer, `21px` inner. Concentric curves.

### Buttons

```css
.btn-primary {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 13px 24px;
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: 100px;         /* Pill shape */
  font-family: 'Outfit', sans-serif;
  font-weight: 600;
  font-size: 0.95rem;
  cursor: pointer;
  transition: transform 0.2s cubic-bezier(0.32,0.72,0,1),
              box-shadow 0.2s cubic-bezier(0.32,0.72,0,1);
}
.btn-primary:hover {
  transform: translateY(-2px);
  box-shadow: 0 12px 32px rgba(79,142,247,0.35);
}
.btn-primary:active {
  transform: translateY(0) scale(0.98);  /* Physical press feedback */
}

/* Icon inside button — never naked */
.btn-icon {
  width: 28px; height: 28px;
  border-radius: 50%;
  background: rgba(255,255,255,0.15);
  display: flex; align-items: center; justify-content: center;
}
```

---

## 7. MOTION & TRANSITIONS

All easing must use custom cubic-bezier. No `linear` or `ease-in-out`:

```css
/* Standard UI transition */
transition: all 0.3s cubic-bezier(0.32, 0.72, 0, 1);

/* Hover lift */
transition: transform 0.25s cubic-bezier(0.34, 1.56, 0.64, 1),
            box-shadow 0.25s cubic-bezier(0.34, 1.56, 0.64, 1);
```

### Scroll Reveal (vanilla JS — no Framer Motion)

```js
// Add to every page
const observer = new IntersectionObserver((entries) => {
  entries.forEach(el => {
    if (el.isIntersecting) {
      el.target.classList.add('visible');
      observer.unobserve(el.target);
    }
  });
}, { threshold: 0.1, rootMargin: '0px 0px -60px 0px' });

document.querySelectorAll('.reveal').forEach(el => observer.observe(el));
```

```css
.reveal {
  opacity: 0;
  transform: translateY(24px);
  transition: opacity 0.7s cubic-bezier(0.32,0.72,0,1),
              transform 0.7s cubic-bezier(0.32,0.72,0,1);
}
.reveal.visible { opacity: 1; transform: translateY(0); }

/* Staggered children */
.reveal:nth-child(2) { transition-delay: 0.1s; }
.reveal:nth-child(3) { transition-delay: 0.2s; }
.reveal:nth-child(4) { transition-delay: 0.3s; }
```

**Rules:**
- NEVER animate `top`, `left`, `width`, `height`. Only `transform` and `opacity`.
- `backdrop-filter` only on fixed/sticky elements (nav, modals). Never on scrolling containers.
- Grain/noise overlays: `position: fixed; pointer-events: none` — never on scroll containers.

---

## 8. FORBIDDEN PATTERNS CHECKLIST

Before shipping any page, verify NONE of these exist:

- [ ] **Inter font** — replace with Outfit/Syne
- [ ] **Purple/violet/pink accents** — replace with `--accent` (#4f8ef7)
- [ ] **Centered hero H1** — must be left-aligned, split layout
- [ ] **3 equal-column card grid** — replace with Bento or zig-zag
- [ ] **Neon outer glow** box-shadows — use tinted inset shadows instead
- [ ] **`height: 100vh`** — replace with `min-height: 100dvh`
- [ ] **Generic card pattern everywhere** — use dividers for flat lists
- [ ] **AI cliché copy** — no "Seamless", "Unleash", "Next-Gen", "Elevate"
- [ ] **Pure black `#000000`** — use `--bg` (#080810)
- [ ] **Gradient text on headlines** — solid color only
- [ ] **Arbitrary z-index: 9999** — use a scale (10/20/30/100/200)
- [ ] **Animations on layout properties** — transform + opacity only
- [ ] **Lorem ipsum or placeholder text** — real copy only

---

## 9. PAGE-BY-PAGE UPGRADE PLAN

| Page | Priority | Key Changes |
|---|---|---|
| `index.html` | HIGH | Asymmetric hero, Bento features, floating nav, new palette |
| `chat-product.html` | HIGH | Product feature layout, demo embed |
| `voice-product.html` | MEDIUM | Same treatment as chat-product |
| `pricing section` | HIGH | 3-tier highlight card (emphasize Growth tier) |
| `about.html` | LOW | Typography upgrade, founder section |
| `contact.html` | MEDIUM | Form with proper validation states |
| `blog.html` + posts | LOW | Editorial layout, proper article typography |

---

## 10. 3D & WEBGL LAYER (Three.js)

For an elegant 3D upgrade — *without* abandoning the vanilla stack.

### 10a. Stack decision (READ FIRST)

Atlyz is vanilla HTML/CSS/JS, **no build step**. That dictates how 3D gets added:

| Approach | Verdict |
|---|---|
| **Vanilla Three.js via CDN `<script>`** | ✅ **USE THIS.** Same model as the GSAP tag already on `voice-product.html`. Zero build. |
| React Three Fiber + Vite (à la `adrianhajdin/iphone`) | ❌ Needs a full React build pipeline — a site rewrite. Study its *ideas*, not its stack. |
| Next.js 14 + Prismic (à la `MokoLaboratoire`) | ❌ Same problem — framework + build step. Not for Atlyz. |

**Reference repos** (what to learn from each):
- `NikitaKarmakarP/3D-Animation-Website` & `Hicham44/3D-portfolio` — vanilla Three.js, CDN-friendly. **Closest match to copy from.**
- `adrianhajdin/iphone` — copy the *techniques only*: scroll-driven camera moves, lighting rigs, real-time material/color swaps. Re-implement in vanilla Three.js + GSAP ScrollTrigger.

Rule: never introduce React, JSX, npm, Vite, or a bundler into this site for the sake of 3D.

### 10b. CDN setup (import map — no bundler)

```html
<script type="importmap">
{ "imports": {
  "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
  "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
}}
</script>
<script type="module">
  import * as THREE from 'three';
  import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
  // ...scene code
</script>
```

### 10c. On-brand concept — the Constellation Hero

The Atlyz logo is already a node graph. The 3D hero should *be* that graph in WebGL:
- A slowly rotating 3D point cloud of nodes connected by faint lines (`--accent` #4f8ef7 + `--green` #3ecf8e).
- Parallax toward the cursor (subtle — max ±6° tilt, matches the card-tilt feel).
- Upgrades the current 2D `#particles-canvas`, doesn't fight it. Pick ONE: replace the 2D canvas on `index.html` hero, keep 2D elsewhere.

Do NOT load heavy `.glb` character models (the demon-bee in the Nikita repo). They're off-brand and slow. Geometry-generated particles/lines only.

### 10d. Performance & accessibility rules (MANDATORY)

- **Mobile**: skip WebGL below 768px → fall back to the existing 2D canvas / static gradient. A 3D scene on a budget Android kills battery and FPS.
- **`prefers-reduced-motion`**: if set, render one static frame, no animation loop.
- **Pause offscreen**: `IntersectionObserver` on the canvas — `cancelAnimationFrame` when the hero scrolls out of view.
- **Cap pixel ratio**: `renderer.setPixelRatio(Math.min(devicePixelRatio, 2))` — never render at 3x on retina.
- **Lazy + non-blocking**: `<script type="module">` is deferred by default; never block first paint on the 3D bundle.
- **Dispose** geometries/materials if a scene is ever torn down. Keep node count ≤ ~150 for the hero.
- **Keep the DOM hero text as real HTML** over the canvas (SEO + accessibility). 3D is decoration behind the copy, never the copy itself.

### 10e. Motion integration

Reuse the GSAP + ScrollTrigger already loaded on product pages. Drive Three.js camera/rotation from a ScrollTrigger timeline rather than a second scroll listener. Respect the dials: `MOTION_INTENSITY: 5` — subtle drift and parallax, **not** spinning showcases or aggressive scroll-jacking.

---

## 11. HOW TO USE THIS SKILL

When redesigning any Atlyz page, give Claude this instruction at the start:

> "Read DESIGN_SKILL.md first. Apply all rules from it. We are upgrading [page name]. The stack is vanilla HTML + CSS + vanilla JS. No React, no Tailwind, no build step."

Then describe what you want changed. Claude will apply all rules in this file automatically.
