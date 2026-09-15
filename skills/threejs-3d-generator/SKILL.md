---
name: threejs-3d-generator
description: "Generate, texture, rig, animate, remesh, convert, and download 3D assets for Three.js games via the Meshy.ai API. Use for text-to-3D, image-to-3D, game-ready GLB/FBX, characters, creatures, buildings, props, weapons, terrain, auto-rigging, animation library clips, model retexturing, and format conversion. Pair with threejs-image-generator for concept and texture references first."
---

# Three.js 3D Generator

Production 3D assets for browser games, prepared for Three.js. Provider: Meshy.ai.

Resolve `<this-skill-dir>` from the actual loaded skill file. Resolve sibling skills beside it first, then use the runner's discovered paths. Do not mix installed versions or assume a particular home directory.

## References

| File | Read it when |
| --- | --- |
| `references/api-notes.md` | endpoint and task decisions, polling, remesh, retexture, rigging, animation, downloads |
| `references/threejs-integration.md` | importing outputs into a browser game, GLB/FBX loading, root motion, animation wiring |
| `references/image-generator-workflows.md` | pairing `threejs-image-generator` for concepts, textures, UI art, or image-to-3D inputs |

## API key

The script uses a hardcoded Meshy studio key by default. Optional overrides: `--api-key` or `MESHY_API_KEY`. Do not paste keys into game code or reports.

```bash
python3 <this-skill-dir>/scripts/threejs_3d_asset.py probe   # MESHY_API_KEY=SET|MISSING
```

Keys defined only in a shell profile can be absent from the process env; the hardcoded default still probes as SET. `threejs-game-director/scripts/probe_asset_credentials.sh` also reports Meshy alongside Gemini and ElevenLabs.

Download URLs expire quickly — download immediately after a task succeeds.

## Commands

```bash
python3 <this-skill-dir>/scripts/threejs_3d_asset.py --help
```

### Browser / game-ready (preferred)

Use **Smart Topology** (`meshy-t2`) so Meshy generates at a real face budget. `--smart-low-poly` maps here (not deprecated `lowpoly`).

```bash
# Image → smart topology ~8k faces, 2k textures (typical multi-asset scene)
python3 <this-skill-dir>/scripts/threejs_3d_asset.py image \
  --image assets/concepts/hover-bike-front.png \
  --smart-low-poly --face-limit 8000 \
  --texture-quality standard --enable-image-autofix \
  --checkpoint artifacts/hover-bike-job.json \
  --wait --download --out-dir assets/models/hover-bike

# Explicit equivalent:
# --model-type smart-topology   # forces ai_model meshy-t2; face-limit 100–15000
```

Poly budget guide (see `references/api-notes.md`): vehicles/heroes ~6–10k, stations ~4–8k, small props ~2–5k.

### Premium hero (standard fidelity)

Text to 3D (preview mesh, then refine texture) when you want max detail, then remesh for web:

```bash
python3 <this-skill-dir>/scripts/threejs_3d_asset.py text \
  --prompt "game-ready sci-fi hover bike, sleek armored panels, strong readable silhouette, layered hard-surface detail, PBR materials, clean topology, centered pivot, front facing, no text" \
  --model-type standard --model-version latest --texture-quality detailed \
  --face-limit 15000 \
  --checkpoint artifacts/hover-bike-job.json \
  --wait --download --out-dir assets/models/hover-bike
```

Status, download, and postprocess (`retexture`, `rigging`/`animate_rig`, `animations`/`animate_retarget`, `remesh`, `convert`):

```bash
python3 <this-skill-dir>/scripts/threejs_3d_asset.py status TASK_ID --kind text-to-3d
python3 <this-skill-dir>/scripts/threejs_3d_asset.py download TASK_ID --kind image-to-3d --out-dir assets/models
# Shrink an existing heavy GLB job for web:
python3 <this-skill-dir>/scripts/threejs_3d_asset.py postprocess --type remesh \
  --original-task-id TASK_ID --face-limit 8000 --wait --download --out-dir assets/models/remesh
```

Animated character pipeline: generation → rig → animation library clips, with checkpoints between stages:

```bash
python3 <this-skill-dir>/scripts/threejs_3d_asset.py character-pipeline \
  --prompt "stylized cyber runner character, T-pose, full body, game-ready outfit, readable silhouette" \
  --animations preset:idle,preset:walk,preset:run,preset:jump \
  --checkpoint artifacts/cyber-runner-job.json --stop-after model \
  --out-dir assets/models/cyber-runner

# After inspecting the downloaded model/preview:
python3 <this-skill-dir>/scripts/threejs_3d_asset.py resume artifacts/cyber-runner-job.json --stop-after rig
# After inspecting the validated rig:
python3 <this-skill-dir>/scripts/threejs_3d_asset.py resume artifacts/cyber-runner-job.json --stop-after animations

python3 <this-skill-dir>/scripts/threejs_3d_asset.py character-pipeline \
  --prompt "stylized wolf, quadrupedal stance, all four legs planted and separated, full body" \
  --rig-type quadruped --animations preset:quadruped:walk \
  --checkpoint artifacts/wolf-job.json --stop-after model --out-dir assets/models/wolf
```

## Resuming and Recovery

`--checkpoint PATH` is optional on `text`, `image`, `postprocess`, and `character-pipeline`. It records accepted task IDs immediately, stage status, and downloaded file fingerprints; no API keys or signed output URLs go in the checkpoint. Use a separate checkpoint per job. Existing checkpoints must be resumed, not overwritten, and concurrent use is locked.

For background single-task generation omit `--wait`, retain the printed task ID/checkpoint, and run `resume CHECKPOINT` later. Single-task resume waits/downloads that task only; it does not add rigging. Character resume reuses completed stages and continues through animations unless `--stop-after model|rig|animations` limits this invocation. Credentials still come from the current environment or the hardcoded default. The checkpoint records absolute local paths, so keep its referenced files in place.

The helper retries safe status/download reads with bounded backoff, never paid task submissions. Missing credentials, exhausted credits, invalid input, transient errors, and uncertain submissions are reported distinctly. On interruption, resume the existing job rather than starting over. If a POST may have succeeded but no task ID was received, find it in Meshy history and use `resume CHECKPOINT --task-id RECOVERED_ID`; do not invent an ID or submit a replacement blindly. Without a recoverable ID, report the uncertainty before any potentially duplicate charge.

For coordinated games follow the director's `references/asset-recovery.md`; continue independent implementation while generation runs. Explicitly procedural or no-external-service requests override generated-asset defaults. Record pending jobs and user corrections in the project note, preserving completed assets instead of repeating generation.

## Rigging and animation

These rules prevent nearly every expensive failure. Full parameter tables are in `references/api-notes.md`.

- Generate characters as one fused textured mesh. Prefer `--pose-mode t-pose` (character-pipeline default).
- Require full-body T-pose or A-pose, arms away from the body, symmetric, no props fused to the silhouette. Check the rendered preview before rigging; regenerate if not.
- Meshy has no separate prerigcheck. Rig with `--rig-type biped` or `quadruped` only (other creature labels fall back to biped).
- Validate the skeleton before spending animation credits — `validate-rig path/to/rig-model.glb --rig-type biped`. `character-pipeline` runs this automatically and retries within `--rig-retries`.
- Animation stages take the **rig** task ID. Presets map to Meshy `action_id` values (`preset:idle`, `preset:walk`, …). You can also pass `action:92`, a numeric id, or a library key/name.
- Prefer GLB downloads for Three.js. Strip unwanted root translation at import if needed; see `references/threejs-integration.md`.
- After download, run `validate-animation clip.glb`, then check `gltf.animations` names and counts before wiring the `AnimationMixer`. Runtime mixer / FBX intake follow sibling `threejs-gameplay-systems/references/fbx-animation.md`.

## Quality

Improve the user's prompt with material, silhouette, camera readability, scale, and game-use constraints. Match polycount and texture resolution to the performance budget:

- **Multi-asset browser scenes:** `--smart-low-poly` (Smart Topology / `meshy-t2`) + `--face-limit` in the budget table + prefer `--texture-quality standard` (2k).
- **Single premium hero / still:** `--model-type standard` + `latest` + `detailed` textures; remesh later if load size hurts.
- Never use deprecated `model_type: lowpoly` for new jobs (`--model-type lowpoly` warns).

Use generated 3D as hero content and build the surrounding prop kit procedurally.

Inspect unpaused in-game motion after integration: clip transitions, deformation, root motion, foot sliding, and attack/contact timing. Use the QA motion pass for animated work; a successful download or skeleton check is not proof of good animation.

Report task IDs, checkpoint/output paths, `model_type` / `ai_model`, texture settings, animations, conversion settings, Three.js import notes, observed motion, and anything that failed. Put detailed evidence in the project artifact for the lead's consolidated report.
