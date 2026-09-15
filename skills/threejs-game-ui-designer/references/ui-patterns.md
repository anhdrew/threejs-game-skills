# Game UI Patterns

Build the game interface, not a web dashboard.

## Hierarchy and states

Order: survival/status → objective/progress → immediate feedback → flavor. Reach for meters, icons, reticles, badges, alert strips, cooldown rings, inventory slots, minimaps, diegetic labels, and compact clusters before generic stat cards. UI stays outside the play path and clear of threats, pickups, the player, and the next decision, and it carries the world's art direction through material cues, color roles, icon shapes, and motion.

Inventory the states before designing: gameplay HUD, pause/resume, settings (audio/accessibility) when useful, fail/retry, win or milestone, loading/empty/error when assets load async, touch controls when mobile is in scope, and debug UI gated separately. A premium game has more than one HUD state.

Where an icon, affordance, or direct interaction can carry the meaning, use it instead of text explaining an obvious control.

## HUD composition

Zones (desktop-primary baseline): top-left for objective, wave, distance, timer, route · top-right for score, currency, combo, inventory, pause · bottom corners for touch movement and action controls · center-top or near-player for short event banners, combos, warnings · near-world for diegetic prompts, target markers, offscreen indicators.

- Fixed-width numeric containers for score, timer, ammo, speed, health, best — values that change width shift layout mid-play.
- Icons plus short labels for unfamiliar resources; meter fills for quantities read at a glance.
- Consistent alert colors across danger, reward, shield, boost, objective, disabled.
- Brief state animation: count-up, meter fill, pulse, slide, snap, ring cooldown.
- Never stack multiple large banners over the play path.

## Mobile-primary game UI

When the title’s primary platform is mobile (phone, mobile portrait, touch-primary — from brief, GDD, pipeline `platform`, or locked portrait play stills), design like a **mobile game**, not a responsive website and not a PC HUD scaled down.

**Composition**
- Portrait-first artboard; design at phone aspect (e.g. ~390×844) and only then widen if desktop is also supported.
- Keep the center play field open. Status lives in thin top/edge strips; verbs and primary CTAs live in the **thumb zone** (lower third / bottom corners).
- Prefer icon + meter + short badge clusters over multi-column stat dashboards, side panels, or mouse-hover tooltips.
- One primary CTA per modal; full-width or large bottom sheets beat tiny centered desktop dialogs.
- Pause / settings / shop entry as edge icon buttons (≥44px), not a top nav bar of text links.

**Touch and reach**
- Every gameplay verb has a visible touch affordance (or a clear on-canvas gesture). No hover-only paths.
- Primary actions within easy thumb reach; secondary actions upper edges. Avoid requiring two-handed precision for the core loop unless the genre demands it (e.g. dual sticks).
- Safe-area insets for notch, status bar, and home indicator on every HUD and overlay state.
- Event banners are short, top-anchored or diegetic — never a stack that covers the thumb zone mid-play.

**Proof**
- Primary evidence is the portrait/mobile capture. A desktop screenshot alone does not prove mobile-primary UI.
- Fail if the layout still reads as a desktop dashboard after shrinking, if touch targets are under ~44px, or if core verbs lack touch intents.

## Menus and overlays

Primary action first (resume, retry, continue, next), then secondary (settings, quit, restart, level select). Restrained panels with meaningful geometry, borders, ticks, glow accents, and material cues — not nested cards or a marketing hero layout inside a game. Icon buttons for pause, sound, restart, fullscreen, settings. Focus, hover, pressed, and disabled states on everything interactive; on mobile-primary, **pressed** is the main affordance and hover is optional. Debug panels sit behind a dev flag or query param.

On mobile-primary menus: large single-column actions, bottom-weighted primary buttons, readable type at phone distance — not a multi-column settings grid copied from desktop.

## Touch controls

Required when mobile is primary or a supported target.

- Pointer events, emitting the same game intents as keyboard and mouse.
- Handle `pointerup`, `pointercancel`, `lostpointercapture`, blur, and visibility change — a missed cancel leaves a control stuck down.
- Safe-area insets; touch targets around 44 CSS pixels; adjacent controls separated enough to prevent mispresses.
- `touch-action` scoped to control regions and the game surface, so page scroll cannot steal input.
- Controls clear of HUD warnings and the play path.
- On mobile-primary titles, place movement/action pads in bottom corners by default unless the locked still or genre shows another thumb layout.

## Responsive constraints

Stable dimensions from CSS variables, `clamp`, grid tracks, fixed icon slots, and fixed-width numerals. Don't scale text purely with viewport width, and avoid negative letter spacing. Check the **primary** viewport first (phone portrait for mobile-primary; desktop for desktop-primary), then secondary targets, using the longest likely values — high score, long labels, multi-digit timers. Nothing clipped, overlapping, unreadably small, or shifting as values change; menus stay reachable on every viewport.

## Style and cohesion

Match the genre: arcade racers need speed and status readability, fighters need health/round/impact hierarchy, exploration needs inventory and objective clarity. A limited status palette over neutral surfaces. Connect UI motifs to world decals, faction marks, vehicle panels, pickups, and hazards. One-note purple/blue gradient UI needs a reason from the game world.

## Generated 2D assets

`threejs-image-generator` covers what hand-coded CSS and icons cannot: faction logos, team crests, title marks; pickup/ability/weapon/inventory/achievement/objective icons; hazard signs, decals, lane glyphs, cockpit labels, item badges; menu, loading, and background plates; GUI material references such as glass panels, metal frames, holographic strips, parchment, tactical screens.

`threejs-3d-generator` is for UI that needs a real 3D object — rotating character preview, vehicle garage, weapon inspect, trophy, diorama, diegetic menu prop.

## State wiring

UI reads from a single source of truth and dispatches intents rather than mutating simulation internals. It updates on pause, restart, resize, orientation, mute, fail/win, score, health, boost, combo, inventory, and accessibility changes — with no stale values after a restart.

## Recurring failures

Generic stat-card HUD · nested cards and oversized decorative panels · **desktop HUD scaled onto a phone for a mobile-primary title** · UI covering threats, pickups, player, or the next decision · text explaining controls that should have been designed as affordances · ignored safe areas · hover-only paths on touch-primary games · touch controls that look right but emit nothing · layout shifting as values change · debug UI shipped as player UI.
