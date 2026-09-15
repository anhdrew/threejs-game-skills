---
name: threejs-game-ui-designer
description: "Design premium Three.js game UI: HUDs, menus, overlays, pause/win/lose screens, settings, icon controls, touch UI, typography, responsive layout, safe areas, text fit, and UI/world cohesion. When the game's primary platform is mobile, design mobile-game UI (portrait-first, thumb zones) — not a desktop HUD scaled down."
---

# Three.js Game UI Designer

Make game UI intentional, readable, responsive, and specific to the genre.

Preserve the user's requested scope and style. For a narrow HUD fix, retain the existing design and check the changed state and target viewports. For a complete UI pass, use the full workflow below. Mobile input is required when mobile is a target, not because a desktop-only game has UI.

## Platform first

Before designing, resolve the **primary platform** from the user brief, GDD, pipeline handoff (`platform` / orientation), or locked play stills:

| Primary platform | UI mandate |
| --- | --- |
| **Mobile** (incl. mobile portrait / phone / touch-primary) | Design **mobile-game UI**: portrait-first composition, thumb-reach zones, large touch affordances, compact HUD clusters. Desktop is a secondary adaptation if supported — never the source layout. |
| **Desktop** (or explicit desktop-only) | Desktop-game UI; add mobile/touch only if mobile is also a supported target. |
| Unclear | Ask once, or infer from portrait masters / `platform: mobile` hypotheses already accepted on the title. |

Mobile-primary means style and layout, not only “also check a phone viewport.” A desktop dashboard shrunk to 390×844 fails this skill for a mobile title. Read the **Mobile-primary game UI** section in `references/ui-patterns.md`.

## Reference

`references/ui-patterns.md` — hierarchy and required states, HUD zones, menus, touch controls, **mobile-primary layout**, responsive constraints, style cohesion, and state wiring. Read it before designing HUDs, menus, overlays, touch controls, or responsive layout.

Load `threejs-image-generator` when logos, icons, GUI art, faction marks, menu backgrounds, or 2D HUD assets would raise the quality. `threejs-3d-generator` only for genuine 3D menu objects and diegetic props, not flat HUD elements.

## Workflow

1. Resolve primary platform (mobile vs desktop). Capture screenshots on the **primary** viewport first (portrait phone for mobile-primary; desktop for desktop-primary), then any secondary targets.
2. Inventory the UI states: gameplay, pause, settings, fail and retry, win or milestone, loading, touch controls (required when mobile is primary or supported).
3. Set the hierarchy: survival and status, then objective, then feedback, then flavor. On mobile-primary, place that hierarchy into thumb zones and edge strips — not a centered desktop panel stack.
4. Replace utility stat cards with authored clusters, meters, badges, icons, alerts, and modal states sized for finger reading at arm’s length.
5. Use stable dimensions, safe-area padding (notch / home indicator), text-fit constraints, and pressed/focus/disabled states. Prefer pressed over hover when touch is primary.
6. Wire UI to game state rather than duplicating game rules inside UI code. Touch controls must emit the same intents as any keyboard/mouse path.
7. Check text fit and overlap with the longest likely values, safe areas, ≥44px touch targets, one-handed reach, and real state changes on the primary viewport (and secondary if in scope).

## What goes wrong

A generic dashboard of stat cards · **desktop HUD shrunk onto a phone for a mobile-primary title** · UI covering the player, threats, or the next decision · text that shifts and clips on mobile · decorative panels that reduce readability · touch controls that look right but emit no intents · hover-only affordances on a touch-primary game.

## Report

UI intent, **primary platform + UI style used**, states covered, files changed, target-viewport screenshots (primary first), text-fit and overlap findings, safe-area and touch-target evidence where applicable, and remaining risks. Feed these results into the lead's consolidated pass rather than rerunning unchanged game-wide checks.
