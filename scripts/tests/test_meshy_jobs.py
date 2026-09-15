"""Offline CLI recovery tests; every provider request is intercepted by FakeMeshy."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import copy
from email.message import Message
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock
from urllib import error, parse


SCRIPT = Path(__file__).resolve().parents[2] / "skills/threejs-3d-generator/scripts/threejs_3d_asset.py"
SPEC = importlib.util.spec_from_file_location("meshy_jobs", SCRIPT)
meshy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(meshy)


def glb(nodes=None, document=None):
    if document is None:
        document = {"asset": {"version": "2.0"}, "nodes": nodes or []}
    payload = json.dumps(document).encode()
    payload += b" " * (-len(payload) % 4)
    return b"glTF" + struct.pack("<II", 2, 20 + len(payload)) + struct.pack("<II", len(payload), 0x4E4F534A) + payload


def mixamo_rig():
    names = ["mixamorig:Hips", "mixamorig:Spine", "mixamorig:Head"]
    for side in ("Left", "Right"):
        for part in ("Arm", "ForeArm", "Hand", "UpLeg", "Leg", "Foot"):
            names.append(f"mixamorig:{side}{part}")
    return glb([{"name": name} for name in names])


def http_error(status, message=None, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    body = json.dumps({"message": message or "error"}).encode()
    return error.HTTPError("https://api.meshy.ai/openapi/v1/image-to-3d", status, "test failure", headers, io.BytesIO(body))


class Response:
    def __init__(self, data, content_type="application/json", status=200):
        self.data = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.headers = {"Content-Type": content_type}
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.data


class FakeMeshy:
    def __init__(self):
        self.posts = []
        self.tasks = {}
        self.assets = {}
        self.status_reads = []
        self.download_reads = []
        self.status_events = {}
        self.download_events = {}
        self.post_events = []
        self.before_post = None
        self.rig_count = 0
        self.bad_rigs = 0

    def close(self):
        queues = [self.post_events, *self.status_events.values(), *self.download_events.values()]
        for queue in queues:
            for event in queue:
                if isinstance(event, error.HTTPError):
                    event.close()

    def output(self, task_id, key, ext, content):
        url = f"https://assets.test/{task_id}/{key}.{ext}?Signature=private-download-signature"
        self.assets[url] = content
        return url

    def event(self, events):
        if not events:
            return None
        value = events.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def __call__(self, req, timeout):
        url = req.full_url
        method = req.get_method()
        if method == "POST" and "/openapi/" in url:
            payload = json.loads(req.data) if req.data else {}
            if self.before_post is not None:
                self.before_post(payload)
            self.posts.append({"url": url, "payload": payload})
            task_id = f"task-{len(self.posts)}"
            if url.endswith("/v2/text-to-3d"):
                kind = "text-to-3d"
                content = glb()
            elif url.endswith("/v1/image-to-3d"):
                kind = "image-to-3d"
                content = glb()
            elif url.endswith("/v1/rigging"):
                kind = "rigging"
                self.rig_count += 1
                content = glb([{"name": "mixamorig:Head"}]) if self.rig_count <= self.bad_rigs else mixamo_rig()
            elif url.endswith("/v1/animations"):
                kind = "animations"
                content = glb()
            elif url.endswith("/v1/retexture"):
                kind = "retexture"
                content = glb()
            elif url.endswith("/v1/remesh"):
                kind = "remesh"
                content = glb()
            elif url.endswith("/v1/convert"):
                kind = "convert"
                content = glb()
            else:
                raise AssertionError(f"Unexpected POST {url}")
            model_url = self.output(task_id, "model", "glb", content)
            preview_url = self.output(task_id, "preview", "png", b"PNG preview")
            task = {
                "id": task_id,
                "status": "SUCCEEDED",
                "progress": 100,
                "kind": kind,
                "model_urls": {"glb": model_url},
                "thumbnail_url": preview_url,
            }
            if kind == "rigging":
                task["result"] = {"rigged_character_glb_url": model_url}
                task.pop("model_urls", None)
            if kind == "animations":
                task["result"] = {"animation_glb_url": model_url}
                task.pop("model_urls", None)
            self.tasks[task_id] = task
            event = self.event(self.post_events)
            if event is not None:
                return Response(event, status=202)
            return Response({"result": task_id}, status=202)
        if method == "GET" and "/openapi/" in url and "/library" not in url:
            task_id = parse.unquote(url.rstrip("/").rsplit("/", 1)[-1])
            self.status_reads.append(task_id)
            event = self.event(self.status_events.get(task_id))
            task = event if event is not None else copy.deepcopy(self.tasks[task_id])
            return Response(task)
        if method == "GET" and url.startswith("https://assets.test/"):
            self.download_reads.append(url)
            self.event(self.download_events.get(url))
            return Response(self.assets[url], "application/octet-stream")
        raise AssertionError(f"Unexpected network request: {method} {url}")


class MeshyJobTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="meshy-jobs-")))
        self.checkpoint = self.root / "job.json"
        self.provider = FakeMeshy()
        self.addCleanup(self.provider.close)
        self.stack.enter_context(mock.patch.object(meshy.request, "urlopen", side_effect=self.provider))
        self.sleeps = self.stack.enter_context(mock.patch.object(meshy.time, "sleep"))
        self.stack.enter_context(mock.patch.dict(os.environ, {"MESHY_API_KEY": "private-api-key"}))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(redirect_stderr(io.StringIO()))

    def run_job(self, *argv):
        command = argv[0]
        cli = list(argv)
        if command in {"text", "image", "postprocess", "character-pipeline"}:
            cli.extend(["--out-dir", str(self.root / "outputs"), "--interval", "0"])
        args = meshy.build_parser().parse_args(cli)
        args.func(args)

    def saved(self):
        return json.loads(self.checkpoint.read_text())

    def text_job(self, *extra):
        self.run_job("text", "--prompt", "paladin", "--checkpoint", str(self.checkpoint), *extra)

    def pipeline(self, *extra):
        self.run_job(
            "character-pipeline",
            "--prompt",
            "paladin",
            "--animations",
            "preset:idle,preset:walk",
            "--checkpoint",
            str(self.checkpoint),
            *extra,
        )

    def resume(self, *extra):
        self.run_job("resume", str(self.checkpoint), *extra)

    def test_probe_reports_set_with_default_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            out = io.StringIO()
            with redirect_stdout(out):
                meshy.cmd_probe(meshy.argparse.Namespace())
            text = out.getvalue()
            self.assertIn("MESHY_API_KEY=SET", text)
            self.assertIn("TRIPO_API_KEY=SET", text)

    def test_text_preview_then_refine_and_resume_without_repost(self):
        self.provider.status_events["task-2"] = [KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.text_job("--wait", "--download")
        self.assertEqual(self.saved()["stages"]["preview"]["task_id"], "task-1")
        self.assertEqual(self.saved()["stages"]["task"]["task_id"], "task-2")
        self.resume()
        stage = self.saved()["stages"]["task"]
        self.assertEqual(stage["state"], "success")
        self.assertTrue(stage["downloads_complete"])
        self.assertEqual(len(self.provider.posts), 2)

    def test_unknown_post_records_intent_and_never_blindly_reposts(self):
        def inspect_intent(_payload):
            stage = self.saved()["stages"]["preview"]
            self.assertEqual(stage["state"], "submitting")
            self.assertNotIn("task_id", stage)

        self.provider.before_post = inspect_intent
        self.provider.post_events = [error.URLError("connection closed after acceptance")]
        with self.assertRaises(meshy.MeshyError) as failed:
            self.text_job()
        self.assertEqual(failed.exception.category, "unknown_submission")
        self.provider.before_post = None
        with self.assertRaises(meshy.MeshyError) as blocked:
            self.resume()
        self.assertEqual(blocked.exception.category, "unknown_submission")
        self.assertEqual(len(self.provider.posts), 1)
        self.resume("--task-id", "task-1")
        self.assertEqual(len(self.provider.posts), 2)
        self.assertEqual(self.saved()["stages"]["preview"]["task_id"], "task-1")

    def test_existing_checkpoint_refuses_new_submission(self):
        self.text_job()
        with self.assertRaises(meshy.MeshyError) as failed:
            self.text_job()
        self.assertEqual(failed.exception.category, "checkpoint_error")
        self.assertEqual(len(self.provider.posts), 2)

    def test_remote_image_checkpoint_has_no_secrets(self):
        self.run_job(
            "image",
            "--image",
            "https://images.test/hero.png?Signature=private-input-signature",
            "--api-key",
            "private-explicit-key",
            "--checkpoint",
            str(self.checkpoint),
            "--wait",
            "--download",
        )
        checkpoint_text = self.checkpoint.read_text()
        for secret in (
            "private-input-signature",
            "private-download-signature",
            "private-api-key",
            "private-explicit-key",
            "https://",
        ):
            self.assertNotIn(secret, checkpoint_text)
        self.assertIsNone(self.saved()["args"]["image"])
        self.assertEqual(len(self.provider.posts), 1)

    def test_single_image_never_implicitly_starts_rigging(self):
        self.run_job("image", "--image", "https://images.test/hero.png", "--checkpoint", str(self.checkpoint))
        with self.assertRaisesRegex(meshy.MeshyError, "requires a character-pipeline"):
            self.resume("--stop-after", "rig")
        self.resume("--stop-after", "model")
        self.assertEqual([p["url"].rsplit("/", 1)[-1] for p in self.provider.posts], ["image-to-3d"])

    def test_pipeline_stops_for_model_and_rig_inspection_then_resumes(self):
        self.pipeline("--stop-after", "model")
        self.assertEqual(len(self.provider.posts), 2)  # preview + refine
        self.assertTrue(self.saved()["stages"]["model"]["downloads_complete"])
        self.resume("--stop-after", "rig")
        self.assertEqual(len(self.provider.posts), 3)
        self.assertEqual(self.saved()["stages"]["rig-1"]["rig_validation"], "valid")
        self.resume()
        self.assertEqual(len(self.provider.posts), 4)
        animation = self.provider.posts[3]["payload"]
        self.assertEqual(animation["rig_task_id"], "task-3")
        self.assertEqual(animation["action_ids"], [0, 30])
        previous_posts = len(self.provider.posts)
        self.resume()
        self.assertEqual(len(self.provider.posts), previous_posts)

    def test_pipeline_reuses_external_model_task(self):
        self.run_job("image", "--image", "https://images.test/hero.png", "--wait", "--download")
        self.run_job(
            "character-pipeline",
            "--model-task-id",
            "task-1",
            "--animations",
            "",
            "--checkpoint",
            str(self.checkpoint),
        )
        self.assertEqual(len(self.provider.posts), 2)
        self.assertTrue(self.provider.posts[1]["url"].endswith("/v1/rigging"))

    def test_paid_rig_retry_budget_survives_interruption(self):
        self.provider.bad_rigs = 2
        self.provider.status_events["task-4"] = [KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.pipeline("--animations", "")
        self.resume()
        self.assertEqual(self.provider.rig_count, 3)
        self.assertEqual(self.saved()["stages"]["rig-3"]["rig_validation"], "valid")

    def test_status_reads_retry_transient_errors_and_honor_retry_after(self):
        self.text_job("--no-texture")
        self.provider.status_events["task-1"] = [http_error(503), http_error(429, retry_after=7), error.URLError("temporary")]
        self.resume()
        self.assertEqual(self.sleeps.call_args_list, [mock.call(1), mock.call(7), mock.call(4)])
        self.assertEqual(len(self.provider.posts), 1)

    def test_hardcoded_key_is_used_when_env_missing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            key = meshy.api_key_from(meshy.argparse.Namespace(api_key=None))
        self.assertEqual(key, meshy.DEFAULT_API_KEY)

    def test_corrupted_completed_file_is_redownloaded_without_generation(self):
        self.text_job("--wait", "--download")
        record = self.saved()["stages"]["task"]["files"]["model"]
        Path(record["path"]).write_bytes(b"corrupted")
        self.resume()
        self.assertTrue(meshy.file_matches(self.saved()["stages"]["task"]["files"]["model"]))
        self.assertEqual(len(self.provider.posts), 2)

    def test_preset_action_ids(self):
        self.assertEqual(meshy.animation_to_action_id("preset:idle"), 0)
        self.assertEqual(meshy.animation_to_action_id("action:92"), 92)
        self.assertEqual(meshy.animation_to_action_id("14"), 14)


if __name__ == "__main__":
    unittest.main()
