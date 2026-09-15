# Three.js Integration

Use this after `threejs-3d-generator` generates or post-processes a model.

Runtime playback of **any** FBX or animated GLB (Meshy, Mixamo, Unity/store-kit) is owned by `threejs-gameplay-systems/references/fbx-animation.md` and `assets/fbx-animation/FbxActor.ts`. This page keeps generator output choices. Do not invent a second loader that strips `animations`.

## Preferred Outputs

- Three.js runtime: GLB/PBR model first.
- Animation/game-engine interchange: FBX when needed, then convert/import carefully.
- Static web asset exchange: GLTF/GLB.
- 3D print only: STL or 3MF, not for textured game runtime.
- Apple AR: USDZ.

## Import Pattern

Use `GLTFLoader`:

```ts
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

const loader = new GLTFLoader();
const gltf = await loader.loadAsync('/assets/models/asset/model.glb');
scene.add(gltf.scene);
```

For animation:

```ts
const mixer = new THREE.AnimationMixer(gltf.scene);
const action = mixer.clipAction(gltf.animations[0]);
action.play();

// in loop
mixer.update(deltaSeconds);
```

Animation intake notes:

- A batched Meshy `action_ids` task returns one file with clips in request order. Map clips by index against the order you requested, rename after load (`clip.name = 'walk'`), then select by your own names.
- Log `gltf.animations.map(c => `${c.name} ${c.tracks.length} tracks`)` after load. A healthy humanoid clip drives many bones; clips with only a handful of tracks mean the upstream auto-rig was degenerate — fix the rig, not the runtime.
- Meshy rigs are typically Mixamo-style (`mixamorig:*` / `Hips`, paired `Left`/`Right` limbs). Prefer keeping the authored tracks; strip root translation carefully if you need in-place playback:
  - Touch ONLY the top root / hips position track.
  - Zero the HORIZONTAL components only — keep Y. Vertical root motion IS the animation for jumps (and the bob in gaits).

```ts
for (const clip of clips) {
  for (const tr of clip.tracks) {
    if (!tr.name.endsWith('.position')) continue;
    if (!/^(Hips|mixamorig:Hips|Root|Armature)\.position$/.test(tr.name) && tr.name !== 'Hips.position') {
      continue;
    }
    const values = tr.values as Float32Array;
    for (let i = 0; i < values.length; i += 3) {
      values[i] = 0;     // x
      values[i + 2] = 0; // z
    }
  }
}
```

## Scale, Pivot, Materials

- Center the asset, set a consistent up-axis, and normalize hero scale against the playable scene.
- Prefer Meshy PBR GLBs (`enable_pbr`) for hero surfaces.
- Keep collision proxies separate from visual meshes.

## Orientation / Wrappers

- Wrong orientation: set a wrapper rotation rather than baking a one-off fix into every consumer.
- For image-to-3D, generate a clear front-facing reference; Meshy does not expose Tripo's `orientation=align_image` flag.

## Failure Modes to Avoid

- Treating a successful download as proof of good in-game motion.
- Stripping most position tracks on a Mixamo/Meshy clip and collapsing the skeleton.
- Re-running paid generation because a signed URL expired — refresh status/download from the existing task id instead.
- Collision too complex: build primitive proxies in Three.js and keep the Meshy mesh visual-only.
