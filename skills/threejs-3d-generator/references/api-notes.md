# Meshy API Notes

These notes summarize the official Meshy OpenAPI used by this skill.

## Base API

- Base URL: `https://api.meshy.ai/openapi`
- Auth: `Authorization: Bearer <MESHY_API_KEY>` (skill ships a hardcoded studio default; override with env/`--api-key`)
- Docs: https://docs.meshy.ai — OpenAPI: https://docs.meshy.ai/openapi.yaml
- Image to 3D: https://docs.meshy.ai/en/api/image-to-3d
- Text to 3D: https://docs.meshy.ai/en/api/text-to-3d
- Remesh: https://docs.meshy.ai/en/api/remesh

Create responses return `{ "result": "<task_id>" }` (HTTP 202). Poll the matching resource path until `status` is terminal.

| Kind | Create / poll |
| --- | --- |
| text-to-3d | `POST/GET /v2/text-to-3d[/{id}]` |
| image-to-3d | `POST/GET /v1/image-to-3d[/{id}]` |
| remesh | `POST/GET /v1/remesh[/{id}]` |
| retexture | `POST/GET /v1/retexture[/{id}]` |
| convert | `POST/GET /v1/convert[/{id}]` |
| rigging | `POST/GET /v1/rigging[/{id}]` |
| animations | `POST/GET /v1/animations[/{id}]` |
| animation library | `GET /v1/animations/library` |
| balance | `GET /v1/balance` |

## Task Status

Ongoing: `PENDING`, `IN_PROGRESS`

Final: `SUCCEEDED`, `FAILED`, `CANCELED`

Query a task with the same API key that created it. Output download URLs expire, so download immediately.

## Checkpointed Jobs

The helper's optional `--checkpoint PATH` works on text, image, postprocess, and character-pipeline commands. IDs are recorded as soon as a task is accepted. `resume PATH` reuses accepted/completed stages and intact downloads; it never turns a single-task job into a character pipeline. Character jobs support `--stop-after model|rig|animations`.

Checkpoint writes are atomic and concurrent access is locked. A saved submission intent with no accepted ID is uncertain. Reconcile with `resume PATH --task-id ID`. Remote input URLs are not stored; `resume PATH --image URL` can re-supply an image source. Local paths and completed files must stay available.

Safe GET operations get up to four attempts with bounded exponential backoff and `Retry-After` handling. Task POSTs are not auto-retried. Errors distinguish missing credentials, rejected credentials, exhausted credits, invalid inputs, transient failures, uncertain submissions, and invalid artifacts.

## Mesh generation modes (`model_type`)

| `model_type` | When to use | `ai_model` | Polycount |
| --- | --- | --- | --- |
| `smart-topology` | **Default for browser / mobile game meshes** — clean topology, native part separation | `meshy-t2` (default; use this) | `target_polycount` **100–15,000** (API default 4,000; skill default **8,000**) |
| `standard` | Premium hero fidelity; then remesh or keep high poly | `latest` / `meshy-7` / `meshy-6`… | With `should_remesh: true`: **100–300,000** (API default 30,000) |
| `lowpoly` | **Deprecated** — Meshy still accepts it but ignores polycount; do not use for new work | ignored | ignored |

Smart Topology ignores `should_remesh`, `topology`, and `save_pre_remeshed_model` (triangle only).  
`ultra_mode` is **standard + meshy-7/latest only**, not smart-topology.

CLI mapping:

| Flag | Payload |
| --- | --- |
| `--smart-low-poly` | `model_type=smart-topology`, `ai_model=meshy-t2`, `--face-limit` default **8000** |
| `--model-type smart-topology` | same as above |
| `--model-type standard` (default) | remesh on; `--face-limit` → `target_polycount` |
| `--model-type lowpoly` | deprecated path; prints a warning |
| `--face-limit N` | `target_polycount` (range depends on mode above) |
| `--texture-quality standard\|detailed\|extreme` | richness + `2k`/`4k`/`8k` |

**Do not** treat `--smart-low-poly` as legacy `model_type: lowpoly` — that mapping was wrong and is fixed in this skill.

### Recommended poly budgets (Three.js browser)

| Role | Prefer | `target_polycount` | Texture |
| --- | --- | --- | --- |
| Hero vehicle / character (1–2 on screen) | smart-topology | 6k–10k | `standard` (2k) or `detailed` (4k) if few heroes |
| Station / large prop | smart-topology | 4k–8k | `standard` (2k) |
| Small cast / prop | smart-topology | 2k–5k | `standard` (2k) |
| Showcase still / art-only | standard + remesh off or high remesh | 30k–100k+ | `detailed` / `extreme` |

Already-generated heavy GLBs: `postprocess --type remesh --face-limit 8000` (or `decimation_mode` 3/4 on the Remesh API).

## Core Workflows

### Text to 3D

Two-step by default:

1. `mode: "preview"` — geometry from `prompt` (apply `model_type` / polycount here)
2. `mode: "refine"` — texture from `preview_task_id`

Useful fields: `ai_model`, `model_type`, `target_polycount`, `topology` (`triangle`|`quad`, standard only), `should_remesh`, `enable_pbr`, `texture_richness`, `texture_resolution` (`2k`|`4k`|`8k`), `pose_mode` (`t-pose`|`a-pose`), `ultra_mode` (standard meshy-7 only).

CLI `--model-version` / `--ai-model` map to `ai_model`. Legacy Tripo version strings are accepted and remapped to `latest`. `--face-limit` maps to `target_polycount`. `--texture-quality` maps to richness + resolution tiers.

### Image to 3D

`POST /v1/image-to-3d` with `image_url` (https URL or data URI). Local files are base64 data-URI encoded by the helper. Same `model_type` / polycount rules as text preview. Also: `should_texture`, `enable_pbr`, `pose_mode`, `image_enhancement`, `texture_resolution`, `ultra_mode`.

### Retexture / Remesh / Convert

- Retexture: `input_task_id` + `text_style_prompt` (or style image / multiview)
- Remesh: topology + `target_polycount` (100–300k) or `decimation_mode` 1–4 (adaptive; ignores exact count). Prefer remesh to shrink existing hero jobs for web.
- Convert: `target_formats` only

Tripo `stylize_model` is not mirrored 1:1 — use remesh/retexture or Meshy Creative Lab endpoints outside this helper.

### Rigging and Animation

Chain: generation task → `rigging` → `animations`.

- Rigging accepts `input_task_id` or `model_url`, and `animation_type`: `biped` (default) or `quadruped`.
- There is no Meshy prerigcheck endpoint; skip that Tripo-era step.
- Animation applies library clips via `action_id` or batched `action_ids` (max 10) onto `rig_task_id`.
- Library: `GET /v1/animations/library` (hundreds of clips). Friendly aliases:

| Alias | Default action_id | Library name |
| --- | --- | --- |
| `preset:idle` | 0 | Idle |
| `preset:walk` | 30 | Casual Walk |
| `preset:run` | 14 | Run 2 |
| `preset:jump` | 466 | Regular Jump |
| `preset:slash` | 97 | Left Slash |
| `preset:shoot` | 98 | Run and Shoot |
| `preset:fall` | 366 | Falling Down |
| `preset:climb` | 444 | Climbing Up Wall |
| `preset:dive` | 506 | Dive Down and Land |
| `preset:hurt` | 178 | Hit Reaction |
| `preset:turn` | 576 | Idle Turn Left |
| `preset:attack` | 4 | Attack |

Also accepted: numeric ids, `action:<id>`, or exact library `key`/`name`.

Rigging also returns optional basic walk/run URLs under `result.basic_animations`.

### Rig validation

After downloading the rig GLB, validate Mixamo-style paired limbs (or legacy Tripo/anatomical names if present) before spending animation credits. `character-pipeline --rig-retries N` retries failed validations.

## Common Output Fields

Normalized by the helper into download keys:

- `model` — primary GLB
- `model_fbx` — FBX when present
- `rendered_image` — thumbnail
- rig/animation-specific extras (`walking_glb`, `running_glb`, …)

Raw Meshy fields live under `model_urls` or `result.*_url`.

## Game Defaults

- **Browser / multi-asset scenes:** `--smart-low-poly` (or `--model-type smart-topology`) + `--face-limit` in budget table + `--texture-quality standard` unless a single hero needs 4k.
- **Art showcase / one hero:** `--model-type standard --model-version latest --texture-quality detailed`; remesh later if file size hurts load time.
- Prefer GLB + PBR for Three.js.
- Character-pipeline defaults to `--pose-mode t-pose`.
