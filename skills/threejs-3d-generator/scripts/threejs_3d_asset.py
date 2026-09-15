#!/usr/bin/env python3
"""Meshy OpenAPI client for skill-driven 3D asset generation."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import base64
import hashlib
from http.client import IncompleteRead
import json
import mimetypes
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Callable, Iterator
from urllib import error, parse, request

BASE_URL = "https://api.meshy.ai/openapi"
# Hardcoded studio key (override with --api-key or MESHY_API_KEY).
DEFAULT_API_KEY = "msy_EF2R3C6PcYps5kfkBN6wcX3WBJ859m9CXB5C"
FINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELED"}
ACTIVE_STATUSES = {"PENDING", "IN_PROGRESS"}

KIND_PATHS = {
    "text-to-3d": "/v2/text-to-3d",
    "image-to-3d": "/v1/image-to-3d",
    "remesh": "/v1/remesh",
    "retexture": "/v1/retexture",
    "rigging": "/v1/rigging",
    "animations": "/v1/animations",
    "convert": "/v1/convert",
}

# Friendly Tripo-era preset aliases -> Meshy action_id (library snapshot).
PRESET_ACTION_IDS: dict[str, int] = {
    "preset:idle": 0,
    "preset:walk": 30,  # Casual Walk
    "preset:run": 14,  # Run 2
    "preset:jump": 466,  # Regular Jump
    "preset:slash": 97,  # Left Slash
    "preset:shoot": 98,  # Run and Shoot
    "preset:fall": 366,  # Falling Down
    "preset:climb": 444,  # Climbing Up Wall
    "preset:dive": 506,  # Dive Down and Land
    "preset:hurt": 178,  # Hit Reaction
    "preset:turn": 576,  # Idle Turn Left
    "preset:attack": 4,
    "preset:biped:idle": 0,
    "preset:biped:walk": 30,
    "preset:biped:run": 14,
    "preset:quadruped:walk": 30,  # best available locomotion; Meshy quad clip set is tiny
}

TEXTURE_QUALITY_TO_RICHNESS = {
    "standard": "medium",
    "detailed": "high",
    "extreme": "high",
}
TEXTURE_QUALITY_TO_RESOLUTION = {
    "standard": "2k",
    "detailed": "4k",
    "extreme": "8k",
}

GET_ATTEMPTS = 4
RETRY_DELAY_CAP = 60
LEGACY_BIPED_PAIRED_BONES = (
    "Clavicle",
    "Upperarm",
    "Forearm",
    "Hand",
    "Thigh",
    "Calf",
    "Foot",
)
MIXAMO_PAIRED = (
    "Arm",
    "ForeArm",
    "Hand",
    "UpLeg",
    "Leg",
    "Foot",
)


class MeshyError(RuntimeError):
    def __init__(self, message: str, category: str = "invalid_input", retry_after: float | None = None):
        super().__init__(message)
        self.category = category
        self.retry_after = retry_after


# Back-compat alias for older tests / call sites.
TripoError = MeshyError


def eprint(*parts: object) -> None:
    print(*parts, file=sys.stderr)


def api_key_from(args: argparse.Namespace | None = None) -> str:
    key = None
    if args is not None:
        key = getattr(args, "api_key", None)
    key = key or os.environ.get("MESHY_API_KEY") or os.environ.get("TRIPO_API_KEY") or DEFAULT_API_KEY
    if not key:
        raise MeshyError("Missing API key. Set MESHY_API_KEY or pass --api-key.", "missing_credentials")
    return key


def error_category(message: str = "", http_status: int | None = None) -> str:
    text = (message or "").lower()
    if http_status == 402 or "credit" in text or "balance" in text or "quota" in text:
        return "exhausted_credits"
    if http_status in {401, 403}:
        return "credentials"
    if http_status in {408, 425, 429} or (http_status is not None and http_status >= 500):
        return "transient"
    return "invalid_input"


def retry_after_seconds(headers: Any) -> float | None:
    value = headers.get("Retry-After") if headers else None
    if value is None:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return None
    return min(RETRY_DELAY_CAP, max(0, delay))


def safe_get(operation: Callable[[], Any]) -> Any:
    for attempt in range(GET_ATTEMPTS):
        try:
            return operation()
        except MeshyError as exc:
            if exc.category != "transient" or attempt + 1 == GET_ATTEMPTS:
                raise
            delay = min(RETRY_DELAY_CAP, max(2 ** attempt, exc.retry_after or 0))
            eprint(f"Transient GET failure; retry {attempt + 1}/{GET_ATTEMPTS - 1} in {delay:g}s")
            time.sleep(delay)


def request_once(req: request.Request, timeout: int) -> tuple[bytes, Any, int]:
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), resp.headers, getattr(resp, "status", 200)
    except error.HTTPError as exc:
        try:
            raw = exc.read()
            data = json.loads(raw) if raw else {}
            message = data.get("message") if isinstance(data, dict) else None
        except (ValueError, OSError, IncompleteRead):
            message = None
        finally:
            exc.close()
        category = error_category(str(message or ""), exc.code)
        if req.get_method() == "GET" and not req.full_url.startswith(BASE_URL + "/") and exc.code in {401, 403}:
            category = "expired_download"
        if req.get_method() != "GET" and category == "transient" and exc.code != 429:
            category = "unknown_submission"
        detail = f" message={message}" if message else ""
        raise MeshyError(
            f"HTTP {exc.code};{detail} {category}.",
            category,
            retry_after_seconds(exc.headers),
        ) from exc
    except (error.URLError, OSError, IncompleteRead) as exc:
        category = "transient" if req.get_method() == "GET" else "unknown_submission"
        raise MeshyError(f"{req.get_method()} interrupted; {category}.", category) from exc


def json_request(
    api_key: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    accept_empty: bool = False,
) -> dict[str, Any] | list[Any] | None:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(f"{BASE_URL}{path}", data=body, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    if payload is not None:
        req.add_header("Content-Type", "application/json")

    def fetch() -> dict[str, Any] | list[Any] | None:
        raw, _headers, status = request_once(req, 60)
        if not raw:
            if accept_empty or status in {202, 204}:
                return None
            category = "transient" if method == "GET" else "unknown_submission"
            raise MeshyError("Empty response from Meshy.", category)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            category = "transient" if method == "GET" else "unknown_submission"
            raise MeshyError("Invalid JSON response from Meshy.", category) from exc
        return data

    return safe_get(fetch) if method == "GET" else fetch()


def resolve_ai_model(value: str | None) -> str:
    if not value or value in {"default", "latest"}:
        return "latest"
    # Legacy Tripo version strings map to Meshy latest.
    if value.startswith("v") or value.startswith("Turbo") or "202" in value:
        return "latest"
    return value


# Smart Topology (meshy-t2) generates directly at target_polycount; range 100–15_000.
SMART_TOPOLOGY_AI = "meshy-t2"
SMART_POLYCOUNT_DEFAULT = 8000
SMART_POLYCOUNT_MAX = 15_000
# Standard remesh target_polycount range 100–300_000 (Meshy default 30_000).
REMESH_POLYCOUNT_MAX = 300_000


def resolve_model_type(args: argparse.Namespace) -> str:
    """Return Meshy model_type. --smart-low-poly aliases smart-topology (not deprecated lowpoly)."""
    explicit = getattr(args, "model_type", None)
    if explicit:
        return explicit
    if getattr(args, "smart_low_poly", False):
        return "smart-topology"
    return "standard"


def map_texture_settings(quality: str | None) -> tuple[str, str]:
    quality = quality or "detailed"
    return (
        TEXTURE_QUALITY_TO_RICHNESS.get(quality, "high"),
        TEXTURE_QUALITY_TO_RESOLUTION.get(quality, "4k"),
    )


def apply_mesh_generation_options(payload: dict[str, Any], args: argparse.Namespace) -> None:
    """Apply model_type / polycount / remesh per current Meshy OpenAPI best practice.

    - smart-topology + meshy-t2: generate at target_polycount (100–15k); remesh/topology ignored
    - standard + should_remesh: remesh to target_polycount (100–300k)
    - lowpoly: deprecated; prefer smart-topology
    """
    model_type = resolve_model_type(args)
    face_limit = getattr(args, "face_limit", None)
    user_ai = getattr(args, "ai_model", None) or getattr(args, "model_version", None)

    if model_type == "smart-topology":
        payload["model_type"] = "smart-topology"
        # Smart Topology only accepts meshy-t2 (default) or legacy meshy-t1.
        if user_ai in {None, "latest", "default", "meshy-7", "meshy-6", "meshy-6-lite", "meshy-5"}:
            payload["ai_model"] = SMART_TOPOLOGY_AI
        else:
            payload["ai_model"] = resolve_ai_model(user_ai)
        poly = SMART_POLYCOUNT_DEFAULT if face_limit is None else face_limit
        if poly < 100 or poly > SMART_POLYCOUNT_MAX:
            raise MeshyError(
                f"smart-topology target_polycount must be 100–{SMART_POLYCOUNT_MAX} (got {poly}).",
                "invalid_input",
            )
        payload["target_polycount"] = poly
        payload.pop("should_remesh", None)
        payload.pop("topology", None)
        if getattr(args, "quad", False):
            eprint("Warning: --quad ignored with smart-topology (triangle output only).")
        if getattr(args, "ultra_mode", False):
            raise MeshyError(
                "ultra_mode is only for standard meshy-7/latest, not smart-topology.",
                "invalid_input",
            )
        return

    if model_type == "lowpoly":
        eprint(
            "Warning: model_type=lowpoly is deprecated by Meshy; "
            "prefer --smart-low-poly / --model-type smart-topology with --face-limit."
        )
        payload["model_type"] = "lowpoly"
        # Meshy ignores ai_model / topology / target_polycount / should_remesh for lowpoly.
        return

    # standard
    payload["model_type"] = "standard"
    payload["ai_model"] = resolve_ai_model(user_ai)
    payload["should_remesh"] = True
    if face_limit is not None:
        if face_limit < 100 or face_limit > REMESH_POLYCOUNT_MAX:
            raise MeshyError(
                f"standard remesh target_polycount must be 100–{REMESH_POLYCOUNT_MAX} (got {face_limit}).",
                "invalid_input",
            )
        payload["target_polycount"] = face_limit
    if getattr(args, "quad", False):
        payload["topology"] = "quad"


def parse_animations(raw: str | list[str] | None) -> list[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def animation_to_action_id(token: str, library: list[dict[str, Any]] | None = None) -> int:
    token = token.strip()
    if token.isdigit():
        return int(token)
    if token.startswith("action:"):
        return int(token.split(":", 1)[1])
    lowered = token.lower()
    if lowered in PRESET_ACTION_IDS:
        return PRESET_ACTION_IDS[lowered]
    if library:
        for item in library:
            if item.get("key") == token or item.get("name") == token:
                return int(item["action_id"])
            if str(item.get("key", "")).lower() == lowered or str(item.get("name", "")).lower() == lowered:
                return int(item["action_id"])
    raise MeshyError(
        f"Unknown animation {token!r}. Use preset:idle|walk|run|jump|…, action:<id>, "
        "a library key/name, or a numeric action_id."
    )


def validate_animations(animations: list[str], rig_type: str | None = None, **_kwargs: Any) -> None:
    for animation in animations:
        if animation.startswith("preset:") and animation not in PRESET_ACTION_IDS and not animation.startswith("preset:biped:"):
            if "attack" in animation and animation != "preset:attack":
                raise MeshyError(
                    f"Unknown animation preset {animation!r}. Use preset:attack, preset:slash, or preset:shoot."
                )
            # Allow unresolved presets; resolve_action_ids will fail with a clearer message later.
            continue
        if rig_type == "quadruped" and animation.startswith("preset:") and animation not in {
            "preset:quadruped:walk",
            "preset:walk",
            "preset:idle",
            "preset:run",
            "preset:jump",
        }:
            eprint(f"Warning: {animation} may not fit a quadruped Meshy rig; proceeding anyway.")


def fetch_animation_library(api_key: str) -> list[dict[str, Any]]:
    data = json_request(api_key, "GET", "/v1/animations/library")
    if not isinstance(data, list):
        raise MeshyError("Animation library response was not a list.", "transient")
    return data


def resolve_action_ids(api_key: str, animations: list[str]) -> list[int]:
    if not animations:
        return []
    need_library = any(
        not token.isdigit()
        and not token.startswith("action:")
        and token.lower() not in PRESET_ACTION_IDS
        for token in animations
    )
    library = fetch_animation_library(api_key) if need_library else None
    return [animation_to_action_id(token, library) for token in animations]


CHECKPOINT_ARGS = set("""
command prompt image negative_prompt model_version ai_model model_seed texture_quality
geometry_quality face_limit no_texture no_pbr smart_low_poly model_type quad auto_size pose_mode
enable_image_autofix texture_alignment orientation type original_task_id texture_prompt
out_format rig_type animation animations no_bake_animation format texture_size
force_symmetry style model_task_id rig_retries force_rig wait download out_dir
interval timeout kind topology target_polycount texture_resolution ultra_mode
""".split())


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


class Checkpoint:
    """Operational journal: no credentials or signed output URLs."""

    def __init__(self, path: Path | None, data: dict[str, Any]):
        self.path = path
        self.data = data

    def save(self) -> None:
        if self.path is not None:
            atomic_write(self.path, json.dumps(self.data, indent=2).encode("utf-8"))

    def stage(self, name: str) -> dict[str, Any]:
        return self.data["stages"].setdefault(name, {"state": "prepared", "files": {}})

    def mark_submitting(self, name: str, request_payload: dict[str, Any]) -> None:
        stage = self.stage(name)
        stage["state"] = "submitting"
        stage["request"] = request_payload
        stage.pop("task_id", None)
        self.save()

    def mark_accepted(self, name: str, task_id: str, *, kind: str) -> None:
        stage = self.stage(name)
        stage["state"] = "accepted"
        stage["task_id"] = task_id
        stage["kind"] = kind
        self.save()

    def mark_unknown(self, name: str) -> None:
        stage = self.stage(name)
        stage["state"] = "unknown_submission"
        self.save()

    def mark_rejected(self, name: str) -> None:
        stage = self.stage(name)
        stage["state"] = "rejected"
        self.save()

    def mark_success(self, name: str, task: dict[str, Any]) -> None:
        stage = self.stage(name)
        stage["state"] = "success"
        stage["task"] = {
            "task_id": task.get("id") or task.get("task_id"),
            "status": task.get("status"),
            "kind": stage.get("kind") or task.get("kind"),
        }
        self.save()


@contextmanager
def checkpoint_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise MeshyError(f"Checkpoint already in use: {path}", "checkpoint_error") from exc
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        lock_path.unlink(missing_ok=True)


@contextmanager
def nullcontext() -> Iterator[None]:
    yield


def create_checkpoint(path: Path | None, command: str, args: argparse.Namespace) -> Checkpoint:
    if path is not None and path.exists():
        raise MeshyError(f"Checkpoint already exists; use resume {path}.", "checkpoint_error")
    stored_args: dict[str, Any] = {"command": command}
    for name in CHECKPOINT_ARGS:
        if hasattr(args, name):
            value = getattr(args, name)
            if name == "image" and isinstance(value, str) and value.startswith(("http://", "https://")):
                stored_args[name] = None
            elif name == "api_key":
                continue
            else:
                stored_args[name] = value
    data = {"version": 2, "provider": "meshy", "args": stored_args, "stages": {}}
    checkpoint = Checkpoint(path, data)
    checkpoint.save()
    return checkpoint


def load_checkpoint(path: Path) -> Checkpoint:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise MeshyError(f"Cannot read checkpoint {path}.", "checkpoint_error") from exc
    if not isinstance(data, dict) or not isinstance(data.get("args"), dict) or not isinstance(data.get("stages"), dict):
        raise MeshyError("Malformed checkpoint: missing args/stages.", "checkpoint_error")
    return Checkpoint(path, data)


def submit_task(api_key: str, kind: str, payload: dict[str, Any], checkpoint: Checkpoint | None, stage_name: str) -> str:
    path = KIND_PATHS[kind]
    request_record: dict[str, Any] = {"kind": kind}
    for key, value in payload.items():
        if key in {"image_url", "texture_image_url", "model_url"}:
            continue
        if isinstance(value, str) and value.startswith(("http://", "https://", "data:")):
            continue
        request_record[key] = value
    if checkpoint is not None:
        checkpoint.mark_submitting(stage_name, request_record)
    try:
        data = json_request(api_key, "POST", path, payload)
    except MeshyError as exc:
        if checkpoint is not None and exc.category == "unknown_submission":
            checkpoint.mark_unknown(stage_name)
        elif checkpoint is not None and exc.category in {"exhausted_credits", "credentials", "invalid_input"}:
            checkpoint.mark_rejected(stage_name)
        raise
    except BaseException:
        if checkpoint is not None:
            checkpoint.mark_unknown(stage_name)
        raise
    task_id = None
    if isinstance(data, dict):
        task_id = data.get("result") or data.get("id")
    if not isinstance(task_id, str) or not task_id:
        if checkpoint is not None:
            checkpoint.mark_unknown(stage_name)
        raise MeshyError("Task response did not include a valid task id; submission outcome is unknown.", "unknown_submission")
    if checkpoint is not None:
        checkpoint.mark_accepted(stage_name, task_id, kind=kind)
    print(task_id)
    return task_id


def get_task(api_key: str, task_id: str, kind: str) -> dict[str, Any]:
    path = f"{KIND_PATHS[kind]}/{parse.quote(task_id)}"
    data = json_request(api_key, "GET", path)
    if not isinstance(data, dict) or not isinstance(data.get("status"), str):
        raise MeshyError("Status response did not include a task status.", "transient")
    data = dict(data)
    data["kind"] = kind
    data["task_id"] = data.get("id") or task_id
    return data


def detect_task(api_key: str, task_id: str, preferred_kind: str | None = None) -> dict[str, Any]:
    kinds = [preferred_kind] if preferred_kind else []
    kinds.extend(k for k in KIND_PATHS if k not in kinds and k is not None)
    errors: list[str] = []
    for kind in kinds:
        if kind is None:
            continue
        try:
            return get_task(api_key, task_id, kind)
        except MeshyError as exc:
            if exc.category in {"credentials", "exhausted_credits"}:
                raise
            errors.append(f"{kind}:{exc.category}")
            continue
    raise MeshyError(f"Could not resolve task {task_id} ({', '.join(errors) or 'no kinds'}).", "invalid_input")


def normalize_outputs(task: dict[str, Any]) -> dict[str, str]:
    outputs: dict[str, str] = {}
    model_urls = task.get("model_urls")
    if isinstance(model_urls, dict):
        for key, url in model_urls.items():
            if isinstance(url, str) and url:
                outputs[key if key != "glb" else "model"] = url
                if key == "glb":
                    outputs["model"] = url
                if key == "fbx":
                    outputs["model_fbx"] = url
    if isinstance(task.get("thumbnail_url"), str) and task["thumbnail_url"]:
        outputs["rendered_image"] = task["thumbnail_url"]
    if isinstance(task.get("alpha_thumbnail_url"), str) and task["alpha_thumbnail_url"]:
        outputs["alpha_thumbnail"] = task["alpha_thumbnail_url"]
    result = task.get("result")
    if isinstance(result, dict):
        mapping = {
            "rigged_character_glb_url": "model",
            "rigged_character_fbx_url": "model_fbx",
            "animation_glb_url": "model",
            "animation_fbx_url": "model_fbx",
            "processed_usdz_url": "usdz",
            "processed_armature_fbx_url": "armature_fbx",
            "walking_glb_url": "walking_glb",
            "running_glb_url": "running_glb",
            "walking_fbx_url": "walking_fbx",
            "running_fbx_url": "running_fbx",
        }
        for src, dest in mapping.items():
            url = result.get(src)
            if isinstance(url, str) and url:
                outputs[dest] = url
        basic = result.get("basic_animations")
        if isinstance(basic, dict):
            for src, dest in mapping.items():
                url = basic.get(src)
                if isinstance(url, str) and url:
                    outputs[dest] = url
    return outputs


def wait_for_task(api_key: str, task_id: str, kind: str, interval: int, timeout: int) -> dict[str, Any]:
    deadline = time.time() + timeout
    while True:
        task = get_task(api_key, task_id, kind)
        status = task.get("status")
        progress = task.get("progress")
        eprint(f"{task_id} {status} {progress}%")
        if status in FINAL_STATUSES:
            if status != "SUCCEEDED":
                message = ""
                err = task.get("task_error")
                if isinstance(err, dict):
                    message = err.get("message") or ""
                category = "task_failed" if status == "FAILED" else "invalid_input"
                if "credit" in message.lower():
                    category = "exhausted_credits"
                raise MeshyError(f"Task {task_id} ended as {status} ({message}).", category)
            return task
        if time.time() >= deadline:
            raise MeshyError(f"Timed out waiting for task {task_id}; resume the accepted task.", "transient")
        time.sleep(max(0, interval))


def file_fingerprint(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {"path": str(path.resolve()), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def file_matches(record: dict[str, Any]) -> bool:
    path = Path(record["path"])
    if not path.is_file():
        return False
    data = path.read_bytes()
    return len(data) == record["size"] and hashlib.sha256(data).hexdigest() == record["sha256"]


def download_url(url: str, dest: Path) -> None:
    req = request.Request(url, method="GET")

    def fetch() -> bytes:
        raw, _headers, _status = request_once(req, 120)
        if not raw:
            raise MeshyError(f"Empty downloaded artifact for {dest.name}.", "invalid_artifact")
        return raw

    content = safe_get(fetch)
    atomic_write(dest, content)


def download_outputs(task: dict[str, Any], out_dir: Path, checkpoint: Checkpoint | None = None, stage_name: str = "task") -> dict[str, dict[str, Any]]:
    outputs = normalize_outputs(task)
    if not outputs:
        raise MeshyError(f"Task is {task.get('status')}; download URLs are available after success.")
    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, Any]] = {}
    if checkpoint is not None:
        existing = checkpoint.stage(stage_name).get("files") or {}
        files.update({k: v for k, v in existing.items() if isinstance(v, dict) and file_matches(v)})
    preferred = ["model", "model_fbx", "rendered_image", "walking_glb", "running_glb"]
    keys = preferred + [k for k in outputs if k not in preferred]
    for key in keys:
        url = outputs.get(key)
        if not url:
            continue
        if key in files and file_matches(files[key]):
            continue
        ext = Path(parse.urlparse(url).path).suffix or {
            "model": ".glb",
            "model_fbx": ".fbx",
            "rendered_image": ".png",
        }.get(key, ".bin")
        dest = out_dir / f"{key}{ext}"
        download_url(url, dest)
        files[key] = file_fingerprint(dest)
        if checkpoint is not None:
            stage = checkpoint.stage(stage_name)
            stage["files"] = files
            stage["downloads_complete"] = False
            checkpoint.save()
    if checkpoint is not None:
        stage = checkpoint.stage(stage_name)
        stage["files"] = files
        stage["downloads_complete"] = True
        checkpoint.save()
    return files


def image_to_data_uri(file_path: Path) -> str:
    if not file_path.is_file():
        raise MeshyError(f"Image not found: {file_path}")
    if file_path.stat().st_size > 20 * 1024 * 1024:
        raise MeshyError("Meshy image input limit is 20MB.")
    mime = mimetypes.guess_type(str(file_path))[0] or "image/png"
    if mime not in {"image/png", "image/jpeg", "image/webp"}:
        raise MeshyError("Image input accepts png, jpeg/jpg, or webp.")
    encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def resolve_image_url(image: str) -> str:
    if image.startswith(("http://", "https://", "data:")):
        return image
    return image_to_data_uri(Path(image))


def glb_document(path: Path) -> dict[str, Any]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MeshyError(f"Cannot read GLB: {path}", "invalid_artifact") from exc
    if len(data) < 20 or data[:4] != b"glTF":
        raise MeshyError(f"Not a complete GLB file: {path}", "invalid_artifact")
    version, length = int.from_bytes(data[4:8], "little"), int.from_bytes(data[8:12], "little")
    if version != 2 or length > len(data):
        raise MeshyError(f"Invalid GLB version or length: {path}", "invalid_artifact")
    offset = 12
    while offset + 8 <= len(data):
        chunk_len = int.from_bytes(data[offset : offset + 4], "little")
        chunk_type = int.from_bytes(data[offset + 4 : offset + 8], "little")
        offset += 8
        if offset + chunk_len > len(data):
            raise MeshyError(f"Truncated GLB chunk: {path}", "invalid_artifact")
        chunk = data[offset : offset + chunk_len]
        offset += chunk_len + (-chunk_len % 4)
        if chunk_type == 0x4E4F534A:
            try:
                document = json.loads(chunk)
            except ValueError as exc:
                raise MeshyError(f"Malformed GLB JSON: {path}", "invalid_artifact") from exc
            if not isinstance(document, dict):
                raise MeshyError(f"No valid JSON object in GLB: {path}", "invalid_artifact")
            return document
    raise MeshyError(f"No valid JSON object in GLB: {path}", "invalid_artifact")


def glb_node_names(path: Path) -> list[str]:
    document = glb_document(path)
    nodes = document.get("nodes") or []
    if not isinstance(nodes, list):
        raise MeshyError(f"Malformed GLB nodes: {path}", "invalid_artifact")
    return [str(node.get("name") or "") for node in nodes if isinstance(node, dict)]


def validate_rig_bones(bones: list[str], rig_type: str) -> list[str]:
    problems: list[str] = []
    names = set(bones)
    mixamo_left = [b for b in bones if "Left" in b and any(part in b for part in MIXAMO_PAIRED)]
    mixamo_right = [b for b in bones if "Right" in b and any(part in b for part in MIXAMO_PAIRED)]
    legacy_left = [b for b in bones if b.startswith("L_")]
    legacy_right = [b for b in bones if b.startswith("R_")]
    tripo_left = [b for b in bones if "_Left_Limb_" in b]
    tripo_right = [b for b in bones if "_Right_Limb_" in b]

    if mixamo_left or mixamo_right or any("mixamo" in b.lower() or b.endswith("Hips") or b == "Hips" for b in bones):
        if len(mixamo_left) < 3 or len(mixamo_right) < 3:
            problems.append(f"asymmetric or short Mixamo limbs L={len(mixamo_left)} R={len(mixamo_right)}")
        if not any("Head" in b for b in bones) and rig_type == "biped":
            problems.append("missing Head bone")
        if not any(b.endswith("Hips") or b == "Hips" or "Hips" in b for b in bones):
            problems.append("missing Hips bone")
        return problems

    if tripo_left or tripo_right:
        if not tripo_left or not tripo_right:
            problems.append("no tripo:: left/right limb pairs")
        return problems

    if legacy_left or legacy_right:
        for part in ("Upperarm", "Thigh"):
            if not any(part in b for b in legacy_left) or not any(part in b for b in legacy_right):
                problems.append(f"missing paired legacy {part}")
        return problems

    if len(bones) < 8:
        problems.append(f"too few bones ({len(bones)}) for a character rig")
    if not names:
        problems.append("no recognizable rig bones")
    return problems


def describe_rig(path: Path, rig_type: str = "biped") -> tuple[str, list[str]]:
    names = [n for n in glb_node_names(path) if n]
    bones = names
    summary = f"{len(bones)} bones: {', '.join(sorted(bones)[:12])}{'…' if len(bones) > 12 else ''}"
    return summary, validate_rig_bones(bones, rig_type)


def cmd_validate_rig(args: argparse.Namespace) -> None:
    summary, problems = describe_rig(Path(args.glb_path), args.rig_type)
    print(summary)
    if problems:
        raise MeshyError("Rig validation failed: " + "; ".join(problems))


def cmd_validate_animation(args: argparse.Namespace) -> None:
    document = glb_document(Path(args.glb_path))
    animations = document.get("animations") or []
    if not animations:
        raise MeshyError("Animation validation failed: no animations in GLB")
    problems: list[str] = []
    for index, clip in enumerate(animations):
        if not isinstance(clip, dict):
            problems.append(f"clip {index} malformed")
            continue
        channels = clip.get("channels") or []
        if len(channels) < 4:
            problems.append(f"clip {index} has only {len(channels)} channels")
    if problems:
        raise MeshyError("Animation validation failed: " + "; ".join(problems))
    print(f"{len(animations)} animation clip(s) ok")


def build_text_payload(args: argparse.Namespace, *, mode: str, preview_task_id: str | None = None) -> dict[str, Any]:
    richness, resolution = map_texture_settings(getattr(args, "texture_quality", None))
    payload: dict[str, Any] = {
        "mode": mode,
        "enable_pbr": not getattr(args, "no_pbr", False),
        "target_formats": ["glb"],
    }
    if mode == "preview":
        payload["prompt"] = args.prompt
        if getattr(args, "negative_prompt", None):
            payload["negative_prompt"] = args.negative_prompt
        apply_mesh_generation_options(payload, args)
        if getattr(args, "pose_mode", None):
            payload["pose_mode"] = args.pose_mode
        if getattr(args, "model_seed", None) is not None:
            payload["seed"] = args.model_seed
        if getattr(args, "auto_size", False):
            payload["auto_size"] = True
        if getattr(args, "ultra_mode", False) and payload.get("model_type") == "standard":
            payload["ultra_mode"] = True
    else:
        # Refine inherits geometry from preview; keep a standard ai_model for texture phase.
        payload["ai_model"] = resolve_ai_model(getattr(args, "ai_model", None) or getattr(args, "model_version", None))
        if resolve_model_type(args) == "smart-topology":
            # Preview used meshy-t2; refine still needs a valid text-to-3d ai_model string.
            payload["ai_model"] = "latest"
        payload["preview_task_id"] = preview_task_id
        payload["texture_richness"] = richness
        payload["texture_resolution"] = getattr(args, "texture_resolution", None) or resolution
        if getattr(args, "texture_prompt", None):
            payload["texture_prompt"] = args.texture_prompt
    return payload


def build_image_payload(args: argparse.Namespace) -> dict[str, Any]:
    _richness, resolution = map_texture_settings(getattr(args, "texture_quality", None))
    payload: dict[str, Any] = {
        "image_url": resolve_image_url(args.image),
        "should_texture": not getattr(args, "no_texture", False),
        "enable_pbr": not getattr(args, "no_pbr", False),
        "target_formats": ["glb"],
    }
    apply_mesh_generation_options(payload, args)
    if getattr(args, "pose_mode", None):
        payload["pose_mode"] = args.pose_mode
    if getattr(args, "enable_image_autofix", False):
        payload["image_enhancement"] = True
    if getattr(args, "texture_prompt", None):
        payload["texture_prompt"] = args.texture_prompt
    if getattr(args, "texture_resolution", None) or not getattr(args, "no_texture", False):
        payload["texture_resolution"] = getattr(args, "texture_resolution", None) or resolution
    if getattr(args, "ultra_mode", False) and payload.get("model_type") == "standard":
        payload["ultra_mode"] = True
    return payload


def finish_task(
    api_key: str,
    task_id: str,
    kind: str,
    args: argparse.Namespace,
    checkpoint: Checkpoint | None,
    stage_name: str,
) -> dict[str, Any]:
    task = get_task(api_key, task_id, kind)
    if getattr(args, "wait", False) or getattr(args, "download", False) or (checkpoint and checkpoint.data["args"].get("command") == "character-pipeline"):
        if task.get("status") not in FINAL_STATUSES:
            task = wait_for_task(api_key, task_id, kind, getattr(args, "interval", 8), getattr(args, "timeout", 600))
    if checkpoint is not None and task.get("status") == "SUCCEEDED":
        checkpoint.mark_success(stage_name, task)
    if getattr(args, "download", False) or (checkpoint and checkpoint.data["args"].get("command") == "character-pipeline"):
        if task.get("status") != "SUCCEEDED":
            task = wait_for_task(api_key, task_id, kind, getattr(args, "interval", 8), getattr(args, "timeout", 600))
            if checkpoint is not None:
                checkpoint.mark_success(stage_name, task)
        out_dir = Path(getattr(args, "out_dir", "meshy-output"))
        download_outputs(task, out_dir, checkpoint, stage_name)
    return task


def cmd_probe(_args: argparse.Namespace) -> None:
    key = os.environ.get("MESHY_API_KEY") or DEFAULT_API_KEY
    status = "SET" if key else "MISSING"
    print(f"MESHY_API_KEY={status}")
    # Keep legacy line so older probes still parse one SET token for 3D.
    print(f"TRIPO_API_KEY={status}")


def cmd_text(args: argparse.Namespace) -> None:
    api_key = api_key_from(args)
    checkpoint = create_checkpoint(Path(args.checkpoint) if args.checkpoint else None, "text", args)
    lock_cm = checkpoint_lock(checkpoint.path) if checkpoint.path else nullcontext()
    with lock_cm:
        if getattr(args, "no_texture", False):
            preview_id = submit_task(api_key, "text-to-3d", build_text_payload(args, mode="preview"), checkpoint, "task")
            args.wait = args.wait or args.download
            finish_task(api_key, preview_id, "text-to-3d", args, checkpoint, "task")
            return
        preview_id = submit_task(api_key, "text-to-3d", build_text_payload(args, mode="preview"), checkpoint, "preview")
        args.wait = True
        finish_task(api_key, preview_id, "text-to-3d", args, checkpoint, "preview")
        refine_id = submit_task(
            api_key,
            "text-to-3d",
            build_text_payload(args, mode="refine", preview_task_id=preview_id),
            checkpoint,
            "task",
        )
        finish_task(api_key, refine_id, "text-to-3d", args, checkpoint, "task")


def cmd_image(args: argparse.Namespace) -> None:
    api_key = api_key_from(args)
    checkpoint = create_checkpoint(Path(args.checkpoint) if args.checkpoint else None, "image", args)
    lock_cm = checkpoint_lock(checkpoint.path) if checkpoint.path else nullcontext()
    with lock_cm:
        task_id = submit_task(api_key, "image-to-3d", build_image_payload(args), checkpoint, "task")
        finish_task(api_key, task_id, "image-to-3d", args, checkpoint, "task")


def cmd_status(args: argparse.Namespace) -> None:
    api_key = api_key_from(args)
    task = detect_task(api_key, args.task_id, getattr(args, "kind", None))
    print(json.dumps(task, indent=2))


def cmd_download(args: argparse.Namespace) -> None:
    api_key = api_key_from(args)
    task = detect_task(api_key, args.task_id, getattr(args, "kind", None))
    if task.get("status") != "SUCCEEDED":
        raise MeshyError(f"Task is {task.get('status')}; download URLs are available after success.")
    files = download_outputs(task, Path(args.out_dir))
    print(json.dumps(files, indent=2))


def postprocess_payload(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    kind_alias = {
        "texture_model": "retexture",
        "retexture": "retexture",
        "animate_rig": "rigging",
        "rig": "rigging",
        "rigging": "rigging",
        "animate_retarget": "animations",
        "retarget": "animations",
        "animate": "animations",
        "animations": "animations",
        "conversion": "convert",
        "convert": "convert",
        "remesh": "remesh",
        "highpoly_to_lowpoly": "remesh",
        "animate_prerigcheck": "prerigcheck",
        "stylize_model": "stylize",
    }
    kind = kind_alias.get(args.type, args.type)
    if kind == "prerigcheck":
        raise MeshyError(
            "Meshy has no prerigcheck endpoint; skip this step and run rigging directly.",
            "invalid_input",
        )
    if kind == "stylize":
        raise MeshyError("Meshy remesh/retexture replace Tripo stylize_model; use --type remesh or retexture.")
    if kind == "retexture":
        if not args.texture_prompt:
            raise MeshyError("--texture-prompt is required for retexture")
        return "retexture", {
            "input_task_id": args.original_task_id,
            "text_style_prompt": args.texture_prompt,
            "enable_pbr": True,
            "ai_model": resolve_ai_model(args.model_version),
            "target_formats": ["glb"],
        }
    if kind == "rigging":
        payload: dict[str, Any] = {
            "input_task_id": args.original_task_id,
            "animation_type": args.rig_type or "biped",
        }
        return "rigging", payload
    if kind == "animations":
        animations = parse_animations(args.animations or args.animation)
        if not animations:
            raise MeshyError("--animation or --animations is required for animate/retarget")
        action_ids = resolve_action_ids(api_key_from(args), animations)
        payload = {"rig_task_id": args.original_task_id}
        if len(action_ids) == 1:
            payload["action_id"] = action_ids[0]
        else:
            payload["action_ids"] = action_ids
        return "animations", payload
    if kind == "convert":
        if not args.format:
            raise MeshyError("--format is required for conversion")
        return "convert", {
            "input_task_id": args.original_task_id,
            "target_formats": [args.format.lower()],
        }
    if kind == "remesh":
        payload = {
            "input_task_id": args.original_task_id,
            "target_formats": ["glb"],
            "topology": "quad" if args.quad else "triangle",
        }
        if args.face_limit:
            if args.face_limit < 100 or args.face_limit > REMESH_POLYCOUNT_MAX:
                raise MeshyError(
                    f"remesh target_polycount must be 100–{REMESH_POLYCOUNT_MAX} (got {args.face_limit}).",
                    "invalid_input",
                )
            payload["target_polycount"] = args.face_limit
        return "remesh", payload
    raise MeshyError(f"Unsupported postprocess type {args.type!r}")


def cmd_postprocess(args: argparse.Namespace) -> None:
    api_key = api_key_from(args)
    kind, payload = postprocess_payload(args)
    checkpoint = create_checkpoint(Path(args.checkpoint) if args.checkpoint else None, "postprocess", args)
    lock_cm = checkpoint_lock(checkpoint.path) if checkpoint.path else nullcontext()
    with lock_cm:
        task_id = submit_task(api_key, kind, payload, checkpoint, "task")
        finish_task(api_key, task_id, kind, args, checkpoint, "task")


def namespace_from_checkpoint(checkpoint: Checkpoint, overrides: argparse.Namespace) -> argparse.Namespace:
    raw = checkpoint.data["args"]
    if not isinstance(raw, dict) or raw.get("command") not in {"text", "image", "postprocess", "character-pipeline"}:
        raise MeshyError("Malformed checkpoint: bad command.", "checkpoint_error")
    for key, value in raw.items():
        if key not in CHECKPOINT_ARGS and key != "command":
            raise MeshyError(f"Malformed checkpoint: unexpected arg {key}.", "checkpoint_error")
        if key in {"interval", "timeout", "face_limit", "rig_retries", "model_seed", "texture_size", "block_size"} and value is not None and type(value) is not int:
            raise MeshyError(f"Malformed checkpoint: {key} must be int.", "checkpoint_error")
        if key in {"wait", "download", "quad", "no_texture", "no_pbr", "force_rig", "auto_size", "ultra_mode", "enable_image_autofix", "smart_low_poly"} and value is not None and type(value) is not bool:
            raise MeshyError(f"Malformed checkpoint: {key} must be bool.", "checkpoint_error")
        if key in {"prompt", "out_dir", "model_version", "ai_model", "texture_quality", "type", "original_task_id", "texture_prompt", "animation", "animations", "rig_type", "pose_mode", "format", "out_format", "model_task_id", "image", "kind"} and value is not None and type(value) is not str:
            raise MeshyError(f"Malformed checkpoint: {key} must be str.", "checkpoint_error")
    args = argparse.Namespace(**{k: raw.get(k) for k in CHECKPOINT_ARGS})
    args.command = raw["command"]
    args.api_key = getattr(overrides, "api_key", None)
    if getattr(overrides, "interval", None) is not None:
        args.interval = overrides.interval
    if getattr(overrides, "timeout", None) is not None:
        args.timeout = overrides.timeout
    if getattr(overrides, "image", None):
        args.image = overrides.image
    args.wait = True
    args.download = True
    if args.out_dir is None:
        args.out_dir = "meshy-output"
    if args.interval is None:
        args.interval = 8
    if args.timeout is None:
        args.timeout = 600
    return args


def ensure_stage_task(checkpoint: Checkpoint, stage_name: str, reconcile_id: str | None, expected_kind: str | None = None) -> str | None:
    stage = checkpoint.stage(stage_name)
    state = stage.get("state")
    if state == "unknown_submission":
        if not reconcile_id:
            raise MeshyError(
                f"Stage {stage_name!r} has an uncertain POST outcome. Find its task ID in Meshy, then "
                f"resume with --task-id.",
                "unknown_submission",
            )
        api_key = api_key_from()
        task = detect_task(api_key, reconcile_id, stage.get("kind") or expected_kind)
        request = stage.get("request") or {}
        if expected_kind and task.get("kind") != expected_kind and request.get("kind") not in {None, expected_kind, task.get("kind")}:
            raise MeshyError(f"Reconciled task {stage_name} does not match the saved submission intent.")
        if request.get("kind") and task.get("kind") != request.get("kind"):
            raise MeshyError(f"Reconciled task {stage_name} does not match the saved submission intent.")
        checkpoint.mark_accepted(stage_name, reconcile_id, kind=task.get("kind") or expected_kind or "text-to-3d")
        return reconcile_id
    if state in {"accepted", "success"} and isinstance(stage.get("task_id"), str):
        return stage["task_id"]
    return None


def resume_single(checkpoint: Checkpoint, args: argparse.Namespace, reconcile_id: str | None) -> None:
    api_key = api_key_from(args)
    command = checkpoint.data["args"]["command"]
    if command == "text":
        preview_id = ensure_stage_task(checkpoint, "preview", reconcile_id, "text-to-3d")
        task_id = ensure_stage_task(checkpoint, "task", None if preview_id and reconcile_id else reconcile_id, "text-to-3d")
        if preview_id and not task_id:
            finish_task(api_key, preview_id, "text-to-3d", args, checkpoint, "preview")
            if checkpoint.data["args"].get("no_texture"):
                return
            refine_id = submit_task(
                api_key,
                "text-to-3d",
                build_text_payload(args, mode="refine", preview_task_id=preview_id),
                checkpoint,
                "task",
            )
            finish_task(api_key, refine_id, "text-to-3d", args, checkpoint, "task")
            return
        if not task_id:
            if command == "text" and args.prompt:
                if getattr(args, "no_texture", False):
                    task_id = submit_task(api_key, "text-to-3d", build_text_payload(args, mode="preview"), checkpoint, "task")
                else:
                    preview_id = submit_task(api_key, "text-to-3d", build_text_payload(args, mode="preview"), checkpoint, "preview")
                    finish_task(api_key, preview_id, "text-to-3d", args, checkpoint, "preview")
                    task_id = submit_task(
                        api_key,
                        "text-to-3d",
                        build_text_payload(args, mode="refine", preview_task_id=preview_id),
                        checkpoint,
                        "task",
                    )
            else:
                raise MeshyError("Nothing to resume for text checkpoint.", "checkpoint_error")
        finish_task(api_key, task_id, "text-to-3d", args, checkpoint, "task")
        return
    if command == "image":
        task_id = ensure_stage_task(checkpoint, "task", reconcile_id, "image-to-3d")
        if not task_id:
            if not args.image:
                raise MeshyError("Remote image URLs are not saved. Resume with --image URL (or a local path).")
            task_id = submit_task(api_key, "image-to-3d", build_image_payload(args), checkpoint, "task")
        finish_task(api_key, task_id, "image-to-3d", args, checkpoint, "task")
        return
    if command == "postprocess":
        task_id = ensure_stage_task(checkpoint, "task", reconcile_id)
        if not task_id:
            kind, payload = postprocess_payload(args)
            task_id = submit_task(api_key, kind, payload, checkpoint, "task")
            finish_task(api_key, task_id, kind, args, checkpoint, "task")
            return
        kind = checkpoint.stage("task").get("kind") or postprocess_payload(args)[0]
        finish_task(api_key, task_id, kind, args, checkpoint, "task")
        return
    raise MeshyError(f"Unsupported checkpoint command {command}.", "checkpoint_error")


def character_model_stage(api_key: str, args: argparse.Namespace, checkpoint: Checkpoint, reconcile_id: str | None) -> str:
    if args.model_task_id:
        checkpoint.mark_accepted("model", args.model_task_id, kind="image-to-3d")
        task = detect_task(api_key, args.model_task_id)
        if task.get("status") != "SUCCEEDED":
            task = wait_for_task(api_key, args.model_task_id, task["kind"], args.interval, args.timeout)
        checkpoint.mark_success("model", task)
        download_outputs(task, Path(args.out_dir) / "model", checkpoint, "model")
        return args.model_task_id

    model_id = ensure_stage_task(checkpoint, "model", reconcile_id, "text-to-3d")
    if model_id:
        kind = checkpoint.stage("model").get("kind") or "text-to-3d"
        task = get_task(api_key, model_id, kind)
        if task.get("status") != "SUCCEEDED":
            task = wait_for_task(api_key, model_id, kind, args.interval, args.timeout)
        checkpoint.mark_success("model", task)
        download_outputs(task, Path(args.out_dir) / "model", checkpoint, "model")
        return model_id

    if not args.prompt:
        raise MeshyError("--prompt is required unless --model-task-id reuses an existing generation task")
    preview_id = ensure_stage_task(checkpoint, "preview", None, "text-to-3d")
    if not preview_id:
        preview_id = submit_task(api_key, "text-to-3d", build_text_payload(args, mode="preview"), checkpoint, "preview")
    finish_task(api_key, preview_id, "text-to-3d", args, checkpoint, "preview")
    refine_id = submit_task(
        api_key,
        "text-to-3d",
        build_text_payload(args, mode="refine", preview_task_id=preview_id),
        checkpoint,
        "model",
    )
    finish_task(api_key, refine_id, "text-to-3d", args, checkpoint, "model")
    return refine_id


def character_rig_stage(api_key: str, args: argparse.Namespace, checkpoint: Checkpoint, model_task_id: str, reconcile_id: str | None) -> str:
    rig_type = args.rig_type or "biped"
    if rig_type not in {"biped", "quadruped"}:
        eprint(f"Meshy rigging supports biped|quadruped only; using biped instead of {rig_type}")
        rig_type = "biped"
    retries = args.rig_retries if args.rig_retries is not None else 2
    selected = checkpoint.data.get("rig_stage")
    if selected and selected in checkpoint.data["stages"]:
        stage = checkpoint.stage(selected)
        if stage.get("rig_validation") in {"valid", "invalid"} and isinstance(stage.get("task_id"), str):
            return stage["task_id"]

    attempt = 1
    while attempt <= retries + 1:
        stage_name = f"rig-{attempt}"
        task_id = ensure_stage_task(checkpoint, stage_name, reconcile_id if attempt == 1 else None, "rigging")
        if not task_id:
            if checkpoint.stage(stage_name).get("state") == "rejected":
                attempt += 1
                continue
            payload = {"input_task_id": model_task_id, "animation_type": rig_type}
            try:
                task_id = submit_task(api_key, "rigging", payload, checkpoint, stage_name)
            except MeshyError as exc:
                if exc.category == "exhausted_credits":
                    raise
                raise
        task = finish_task(api_key, task_id, "rigging", args, checkpoint, stage_name)
        files = checkpoint.stage(stage_name).get("files") or {}
        model_file = files.get("model")
        if not model_file:
            raise MeshyError("Rig task succeeded but no GLB was downloaded.", "invalid_artifact")
        summary, problems = describe_rig(Path(model_file["path"]), rig_type)
        eprint(summary)
        if problems and not args.force_rig and attempt <= retries:
            checkpoint.stage(stage_name)["rig_validation"] = "invalid"
            checkpoint.save()
            attempt += 1
            reconcile_id = None
            continue
        checkpoint.stage(stage_name)["rig_validation"] = "invalid" if problems else "valid"
        checkpoint.data["rig_stage"] = stage_name
        checkpoint.save()
        if problems and not args.force_rig:
            raise MeshyError("Rig validation failed: " + "; ".join(problems))
        return task_id
    raise MeshyError("Rig retries exhausted without a valid skeleton.", "invalid_artifact")


def character_animation_stage(api_key: str, args: argparse.Namespace, checkpoint: Checkpoint, rig_task_id: str, reconcile_id: str | None) -> None:
    animations = parse_animations(args.animations)
    validate_animations(animations, args.rig_type)
    if not animations:
        return
    action_ids = resolve_action_ids(api_key, animations)
    # Prefer one batched Meshy task.
    stage_name = "animation-1"
    task_id = ensure_stage_task(checkpoint, stage_name, reconcile_id, "animations")
    if not task_id:
        payload: dict[str, Any] = {"rig_task_id": rig_task_id}
        if len(action_ids) == 1:
            payload["action_id"] = action_ids[0]
        else:
            payload["action_ids"] = action_ids
        task_id = submit_task(api_key, "animations", payload, checkpoint, stage_name)
    finish_task(api_key, task_id, "animations", args, checkpoint, stage_name)


def run_character_pipeline(args: argparse.Namespace, checkpoint: Checkpoint, stop_after: str, reconcile_id: str | None) -> None:
    api_key = api_key_from(args)
    model_task_id = character_model_stage(api_key, args, checkpoint, reconcile_id)
    if stop_after == "model":
        return
    rig_task_id = character_rig_stage(api_key, args, checkpoint, model_task_id, reconcile_id)
    if stop_after == "rig":
        return
    character_animation_stage(api_key, args, checkpoint, rig_task_id, reconcile_id)


def cmd_character_pipeline(args: argparse.Namespace) -> None:
    args.wait = True
    args.download = True
    if getattr(args, "pose_mode", None) is None:
        args.pose_mode = "t-pose"
    checkpoint = create_checkpoint(Path(args.checkpoint) if args.checkpoint else None, "character-pipeline", args)
    lock_cm = checkpoint_lock(checkpoint.path) if checkpoint.path else nullcontext()
    with lock_cm:
        run_character_pipeline(args, checkpoint, args.stop_after or "animations", None)


def cmd_resume(args: argparse.Namespace) -> None:
    path = Path(args.checkpoint)
    with checkpoint_lock(path):
        checkpoint = load_checkpoint(path)
        ns = namespace_from_checkpoint(checkpoint, args)
        command = checkpoint.data["args"]["command"]
        stop_after = getattr(args, "stop_after", None)
        if command == "character-pipeline":
            run_character_pipeline(ns, checkpoint, stop_after or "animations", getattr(args, "task_id", None))
            return
        if stop_after in {"rig", "animations"}:
            raise MeshyError("--stop-after rig|animations requires a character-pipeline checkpoint. Single jobs never add paid stages.")
        if getattr(args, "image", None) and command != "image":
            raise MeshyError("--image is only for resuming an image submission.")
        uncertain = [name for name, stage in checkpoint.data["stages"].items() if stage.get("state") == "unknown_submission"]
        if getattr(args, "task_id", None) and len(uncertain) > 1:
            raise MeshyError("--task-id requires exactly one uncertain submission in the checkpoint.")
        resume_single(checkpoint, ns, getattr(args, "task_id", None))


def add_common_model_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--negative-prompt")
    parser.add_argument("--model-version", default="latest", help="Meshy ai_model (latest|meshy-7|meshy-t2|…); legacy Tripo version strings map to latest")
    parser.add_argument("--ai-model", help="alias of --model-version")
    parser.add_argument("--model-seed", type=int)
    parser.add_argument("--texture-quality", choices=["standard", "detailed", "extreme"], default="detailed")
    parser.add_argument("--texture-resolution", choices=["2k", "4k", "8k"])
    parser.add_argument("--geometry-quality", choices=["standard", "detailed"])
    parser.add_argument(
        "--face-limit",
        type=int,
        help="Meshy target_polycount: 100–15000 for smart-topology; 100–300000 for standard remesh",
    )
    parser.add_argument("--no-texture", action="store_true")
    parser.add_argument("--no-pbr", action="store_true")
    parser.add_argument(
        "--model-type",
        choices=["standard", "smart-topology", "lowpoly"],
        help="Meshy model_type (default: standard). Prefer smart-topology for browser/game budgets",
    )
    parser.add_argument(
        "--smart-low-poly",
        action="store_true",
        help="alias for --model-type smart-topology (meshy-t2); default --face-limit 8000; not deprecated lowpoly",
    )
    parser.add_argument("--quad", action="store_true", help="quad topology (standard remesh only; ignored for smart-topology)")
    parser.add_argument("--auto-size", action="store_true")
    parser.add_argument("--pose-mode", choices=["a-pose", "t-pose"])
    parser.add_argument("--ultra-mode", action="store_true", help="standard meshy-7/latest only")
    parser.add_argument("--texture-prompt")


def add_shared_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-key")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--out-dir", default="meshy-output")
    parser.add_argument("--interval", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--checkpoint", metavar="PATH", help="create a resumable job journal; existing paths require resume")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Meshy OpenAPI 3D asset helper")
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="print MESHY_API_KEY=SET|MISSING")
    probe.set_defaults(func=cmd_probe)

    text = sub.add_parser("text", help="submit text-to-3d (preview + refine)")
    text.add_argument("--prompt", required=True)
    add_common_model_args(text)
    add_shared_runtime_args(text)
    text.set_defaults(func=cmd_text)

    image = sub.add_parser("image", help="submit image-to-3d from local path, URL, or data URI")
    image.add_argument("--image", required=True)
    image.add_argument("--enable-image-autofix", action="store_true")
    image.add_argument("--texture-alignment", choices=["original_image", "geometry"])
    image.add_argument("--orientation", choices=["default", "align_image"])
    add_common_model_args(image)
    add_shared_runtime_args(image)
    image.set_defaults(func=cmd_image)

    status = sub.add_parser("status", help="get task status")
    status.add_argument("task_id")
    status.add_argument("--kind", choices=sorted(KIND_PATHS))
    status.add_argument("--api-key")
    status.set_defaults(func=cmd_status)

    download = sub.add_parser("download", help="download successful task outputs")
    download.add_argument("task_id")
    download.add_argument("--kind", choices=sorted(KIND_PATHS))
    download.add_argument("--api-key")
    download.add_argument("--out-dir", default="meshy-output")
    download.set_defaults(func=cmd_download)

    resume = sub.add_parser("resume", help="resume a checkpoint; wait/download single tasks or continue the saved character chain")
    resume.add_argument("checkpoint", metavar="CHECKPOINT")
    resume.add_argument("--api-key")
    resume.add_argument("--interval", type=int)
    resume.add_argument("--timeout", type=int)
    resume.add_argument(
        "--stop-after",
        choices=["model", "rig", "animations"],
        help="character checkpoint stage limit for this invocation (default: animations); single text/image jobs accept only model",
    )
    resume.add_argument("--task-id", help="reconcile the one uncertain POST using its independently recovered Meshy task ID; never resubmit it")
    resume.add_argument("--image", help="re-supply an unsaved remote image URL or local path if the image task was never accepted")
    resume.set_defaults(func=cmd_resume)

    post = sub.add_parser("postprocess", help="submit Meshy remesh/retexture/rigging/animations/convert task")
    post.add_argument("--type", required=True)
    post.add_argument("--original-task-id", required=True)
    post.add_argument("--model-version")
    post.add_argument("--texture-prompt")
    post.add_argument("--texture-quality", choices=["standard", "detailed", "extreme"])
    post.add_argument("--out-format", choices=["glb", "fbx"])
    post.add_argument("--rig-type", choices=["biped", "quadruped", "hexapod", "octopod", "avian", "serpentine", "aquatic"])
    post.add_argument("--spec", choices=["meshy", "mixamo", "tripo"])
    post.add_argument("--animation")
    post.add_argument("--animations")
    post.add_argument("--animate-in-place", action="store_true")
    post.add_argument("--no-bake-animation", action="store_true")
    post.add_argument("--no-export-with-geometry", action="store_true")
    post.add_argument("--format", choices=["GLTF", "USDZ", "FBX", "OBJ", "STL", "3MF", "GLB", "BLEND"])
    post.add_argument("--face-limit", type=int)
    post.add_argument("--texture-size", type=int)
    post.add_argument("--quad", action="store_true")
    post.add_argument("--force-symmetry", action="store_true")
    post.add_argument("--flatten-bottom", action="store_true")
    post.add_argument("--flatten-bottom-threshold", type=float)
    post.add_argument("--style", choices=["lego", "voxel", "voronoi", "minecraft"])
    post.add_argument("--block-size", type=int)
    add_shared_runtime_args(post)
    post.set_defaults(func=cmd_postprocess)

    validate = sub.add_parser("validate-rig", help="check a downloaded rig GLB for degenerate auto-rig skeletons")
    validate.add_argument("glb_path")
    validate.add_argument("--rig-type", default="biped", choices=["biped", "quadruped", "hexapod", "octopod", "avian", "serpentine", "aquatic"])
    validate.set_defaults(func=cmd_validate_rig)

    validate_anim = sub.add_parser("validate-animation", help="basic QA for animation GLBs")
    validate_anim.add_argument("glb_path")
    validate_anim.set_defaults(func=cmd_validate_animation)

    pipeline = sub.add_parser("character-pipeline", help="generate, rig, animate, and download a character via Meshy")
    pipeline.add_argument("--prompt")
    pipeline.add_argument("--model-task-id", help="reuse an existing generation task instead of generating (skips --prompt)")
    pipeline.add_argument("--rig-retries", type=int, default=2, help="extra rig attempts when validation fails (default 2)")
    pipeline.add_argument("--rig-model-version", default=None, help="ignored on Meshy; retained for CLI compatibility")
    pipeline.add_argument("--animations", default="preset:idle,preset:walk,preset:run")
    pipeline.add_argument("--model-version", default="latest")
    pipeline.add_argument("--ai-model")
    pipeline.add_argument("--texture-quality", default="detailed", choices=["standard", "detailed", "extreme"])
    pipeline.add_argument("--geometry-quality", default="standard", choices=["standard", "detailed"])
    pipeline.add_argument("--face-limit", type=int)
    pipeline.add_argument("--pose-mode", choices=["a-pose", "t-pose"], default="t-pose")
    pipeline.add_argument("--rig-type", choices=["biped", "quadruped", "hexapod", "octopod", "avian", "serpentine", "aquatic"])
    pipeline.add_argument("--spec", default="meshy", choices=["meshy", "mixamo", "tripo"])
    pipeline.add_argument("--force-rig", action="store_true", help="continue even if rig validation reports problems")
    pipeline.add_argument("--animate-in-place", action="store_true")
    pipeline.add_argument("--api-key")
    pipeline.add_argument("--out-dir", default="meshy-character")
    pipeline.add_argument("--interval", type=int, default=8)
    pipeline.add_argument("--timeout", type=int, default=900)
    pipeline.add_argument("--checkpoint", metavar="PATH", help="create a resumable character chain; accepted stages are never regenerated on resume")
    pipeline.add_argument(
        "--stop-after",
        choices=["model", "rig", "animations"],
        default="animations",
        help="stop after downloading this stage for inspection (default: animations); use --checkpoint to continue later",
    )
    pipeline.set_defaults(func=cmd_character_pipeline)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except MeshyError as exc:
        eprint(f"threejs_3d_asset.py: [{exc.category}] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
