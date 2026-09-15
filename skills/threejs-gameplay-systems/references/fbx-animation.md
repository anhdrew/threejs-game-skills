# FBX / Skinned Animation

Play imported skeletal clips in the game loop. This is the runtime playbook for Mixamo libraries, Unity/Asset-Inventory FBX, store-kit bodies, and Meshy rigged/animated FBX/GLB exports.

Generation and bake rules stay in `threejs-3d-generator/references/threejs-integration.md`. Import cleanup stays in `threejs-aaa-graphics-builder`. Mixer, clip catalog, and verb wiring live here.

Copy `../assets/fbx-animation/FbxActor.ts` into the game (`src/systems/FbxActor.ts`) when any skinned FBX/GLB body must move. Do not re-author a parallel loader that empties `animations`.

## When this is in scope

Read this before placing a skinned FBX or an animated GLB in `?mode=play` or lookdev isolate.

- The file has `animations.length > 0`, or companion clip FBX sit next to a T-pose mesh.
- A roster verb (walk, serve, cheer, attack) should be visible on the body, not only on the HUD.
- Crowds or repeats of the same rig.

A static prop FBX with no takes can stay a mesh stamp. A character FBX that still looks like a T-pose after load is unfinished work, not a style choice.

## Hard rules

1. **Never assign `root.animations = []`** to "simplify" a mesh. That is how store-kit bodies freeze in bind pose.
2. **One `AnimationMixer` per instance**, rooted on that instance's graph. Share the loaded template; do not share a mixer.
3. **Clone skinned graphs with `SkeletonUtils.clone`.** `Object3D.clone()` / `mesh.clone()` leaves skins bound to the template skeleton.
4. **`mixer.update(deltaSeconds)`** — the scaffold loop already passes seconds. Passing milliseconds plays clips ~60× too fast.
5. **Touch only the top root bone's position track** when stripping travel (`Hips` / `Root` / `mixamorigHips`). Zeroing every `*.position` track hunches the skeleton.
6. Play clips otherwise untouched. Do not delete twist-bone tracks. Prefer in-place conversion at import (zero horizontal root/hips translation) rather than provider-side in-place flags.
7. Do not gate the primary verb on `playOnce` finishing. Juice can sync to the clip; input cannot wait for it.

## Ownership

| Concern | Owner |
| --- | --- |
| Load FBX, texture aliases, Phong → standard, scale, shadows | graphics + this helper |
| Clip catalog, crossfade, root-motion lock, mixer in `update` | gameplay (this file) |
| Meshy generate / rig / animate / `validate-animation` | `threejs-3d-generator` |
| Motion evidence, frozen-rig defects | `threejs-qa-release` + `threejs-debug-profiler` |

## Three intake shapes

**Embedded takes.** One mesh FBX, several `animations[]`. `FBXLoader` often emits each take twice with different node-path prefixes (`Armature.001|walk` vs `Armature|Armature.001|walk`). Keep the **shallower** path (fewer `|`). Rename by the take suffix.

**Mixamo / one-file-per-clip.** T-pose (or idle) mesh FBX plus `idle.fbx`, `walk.fbx`, `run.fbx`. For each clip file use `pickShallowClips(...)[0]` — equivalently `animations[0]` after the shallow filter. The variant order of duplicate takes is unstable; index `0` on the raw array is only safe after that filter. Bind tracks onto the **mesh instance**, not the clip file's leftover mesh.

**Unity / store-kit.** Same two shapes. Textures are referenced by basename (`people_pal.png`); remap with `LoadingManager.setURLModifier`. Humanoids exported in centimeters often load ~100× too big — if no `height` is given and the mesh Y exceeds ~8 units, scale by `0.01`. Companion animation FBX belong in the clip map, not as extra scene meshes.

Meshy humanoid exports are usually Mixamo-style skeletons in GLB or FBX. Use this helper, then apply only the 3D generator integration note's root-motion strip rules when you need in-place playback.

## Loader

`FBXLoader` is `three/addons/loaders/FBXLoader.js`. It imports `fflate`. Vite/npm games get it automatically. Import-map / CDN pages must map `"fflate"` (for example `https://cdn.jsdelivr.net/npm/fflate@0.8.2/esm/browser.js`).

```ts
import { FBXLoader } from 'three/addons/loaders/FBXLoader.js';
import { clone as cloneSkinned } from 'three/addons/utils/SkeletonUtils.js';

const manager = new THREE.LoadingManager();
manager.setURLModifier((url) => {
  const name = url.split(/[\\/]/).pop()?.split('?')[0]?.toLowerCase() ?? url;
  return aliases[name] ?? url;
});
const loader = new FBXLoader(manager);
const template = await loader.loadAsync('/models/hero.fbx');
```

After load, log `template.animations.map((c) => `${c.name} ${c.duration.toFixed(2)}s ${c.tracks.length}tr`)`. A healthy humanoid clip drives many bones. A handful of tracks means a bad bind or a degenerate rig — fix the catalog, not the HUD.

`SkinnedMesh.frustumCulled = false` unless you have proven bounds. FBXLoader materials are Phong; convert to `MeshStandardMaterial` (keep maps) so the game lighting rig matches GLB heroes. For shipped art parity, converting FBX→GLB offline is optional and the corrected clips survive that conversion.

## Bind and instance

```ts
const instance = cloneSkinned(template);
const mixer = new THREE.AnimationMixer(instance);
const clip = bindClipToGraph(sourceClip.clone(), instance);
mixer.clipAction(clip).play();
```

Track paths from a second FBX rarely match the mesh graph 1:1 (`mixamorig:Hips` vs `mixamorigHips` vs `Armature|mixamorig:Hips`). Remap each track onto a bone by **normalized suffix**, then rewrite `track.name` to `BoneName.property`. Drop tracks that do not bind. If a clip binds zero tracks, it is a skeleton mismatch — do not `clipAction` it and hope.

`SkeletonUtils.retargetClip` is for different skeleton conventions (Mixamo → custom), not for the prefix cleanup above. Supply bone maps and check bind-pose orientation when you actually retarget.

Scale and yaw live on a **wrapper group**. The mixer root is the inner clone. Gameplay position/rotation moves the wrapper so root-motion lock and controller motion do not fight.

## Root motion

Keep exported travel intact, then lock it at import when a character controller already moves XZ.

- `lock-xz` (default for arcade controllers): pin the root bone's XZ to the first key; keep Y so jumps and gait bob remain.
- `lock-xyz`: fully in-place (idle, emotes).
- `keep`: cinematic or when the clip is the only mover.

Only the top root bone (`Root`, `Hips`, `Pelvis`, `mixamorigHips`). Never pattern-match every position track.

```ts
for (const track of clip.tracks) {
  if (!isRootPosition(track.name)) continue;
  const v = track.values, x0 = v[0], z0 = v[2];
  for (let i = 0; i < v.length; i += 3) { v[i] = x0; v[i + 2] = z0; }
}
```

Mutate a **clone** of the clip. Shared clip objects plus in-place strip will corrupt later stamps.

## Loop and feel

Update every live mixer from the same `update(delta, elapsed)` as the rest of the game. Honor `setReducedMotion` / screenshot pause by passing `0` as the mixer delta so baselines stay still.

```ts
actor.update(reducedMotion || pausedForScreenshot ? 0 : delta);
```

Locomotion: `idle` ↔ `walk` ↔ `run` with 0.12–0.25s `fadeIn`/`fadeOut`. One-shot attacks use `LoopOnce` + `clampWhenFinished` and return to the locomotion clip; the attack input itself is already consumed. Crossfade — do not `stopAllAction()` on every frame or the bind pose pops.

Select clips by **name / keyword** (`idle`, `walk`, `run`, `slash`/`attack`), not by raw array index, except for a single-take file after the shallow filter.

Crowd: one `FbxRig.load`, then `stamp()` per body. Each stamp gets its own mixer. Offset `mixer.time` slightly if many idles start on the same frame.

## Helper

```ts
import { FbxRig } from './systems/FbxActor';

const people = await FbxRig.load({
  url: '/models/floor-host.fbx',
  clips: {
    idle: '/models/anim-idle.fbx',
    walk: '/models/anim-walk.fbx',
  },
  height: 1.72,
  textureAliases: { 'people_pal.png': '/textures/people_pal.png' },
});

const host = people.stamp('floor-host');
scene.add(host.group);
host.play('idle');
// in Game.update:
host.update(animDelta);
```

`FbxRig` owns the loaded template and prepared clips. `FbxActor` is one instance: wrapper, mixer, `play` / `playOnce`, `debugState()`.

Publish `debugState()` onto `__THREE_GAME_DIAGNOSTICS__.animation` for the profiler.

## Failures

| Symptom | Usual cause |
| --- | --- |
| T-pose / bind pose forever | clips stripped; mixer never updated; `play` never called; 0 tracks bound |
| Mesh explodes or is invisible on the second copy | `Object3D.clone` instead of `SkeletonUtils.clone` |
| Body hunches / limbs collapse | stripped non-root position tracks, or twist tracks deleted |
| Slides while the controller also moves | root motion kept and applied twice |
| Darker than GLB heroes | leftover Phong, no standard conversion, missing lights |
| Textures 404 | Unity relative paths, no URL modifier / alias map |
| 100× giant character | centimeters, no height fit / 0.01 fallback |
| Jittery swap between clips | `stopAllAction` every frame; fade 0; two mixers on one graph |
| Clip plays on the wrong body | mixer rooted on the template, or shared mixer across stamps |
| CDN `Failed to resolve fflate` | import map missing |

Different skeleton families (Mixamo vs custom vs Meshy) will not bind by suffix alone. Retarget in a DCC or with `SkeletonUtils.retargetClip`; do not average bones in gameplay code.

## Evidence

Unpaused motion at the gameplay camera: one full locomotion cycle, a start/stop or idle↔walk crossfade, plus the verb clip if combat/work is in the slice. Still screenshots cannot pass this. Record clip names, durations, and the current mixer action next to the capture.
