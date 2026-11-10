"""Tool images are retained as evidence, without recapture or external save calls."""

import asyncio
import base64
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from PIL import Image

from assistant_runtime.api import ExecutionRequest
from assistant_runtime.api.ai_usage import UsageScope, current_usage
from assistant_runtime.app.store import Store
from assistant_runtime.core.workfiles import WorkFiles
from assistant_runtime.core.pi.conversation import task_event, retained_native_media_item


def picture():
    data = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(data, format="PNG")
    raw = data.getvalue()
    return raw, {"mime_type": "image/png", "data": base64.b64encode(raw).decode()}


class ToolMediaTest(unittest.IsolatedAsyncioTestCase):
    async def test_native_hidden_tool_delivers_only_retained_media(self):
        receipt = {"state": "ready", "attachments": [{"id": "a" * 32, "type": "image/png"}], "issues": []}
        raw = {
            "content": [{"type": "image", "data": "private-raw-data"}],
            "details": {"personal_agent.tool_media": receipt},
        }
        item = retained_native_media_item("call", "read", raw)
        event = task_event("activity.completed", {"item": {**item, "status": "completed"}})
        self.assertEqual(event["attachments"], receipt["attachments"])
        self.assertTrue(event["detail"])
        self.assertNotIn("private-raw-data", json.dumps(item))
        self.assertNotIn("arguments", item)
        self.assertIsNone(retained_native_media_item("ordinary", "read", {"content": []}))

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.db")
        self.files = WorkFiles(self.root, self.store.db)
        self.raw, self.image = picture()

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def register(self, images=None, call="call"):
        return await self.files.tool_media.register(
            "run", call, images if images is not None else [self.image], "executions/run", owner="caller"
        )

    async def test_original_bytes_reference_and_idempotency_across_restart(self):
        first, same = await asyncio.gather(
            self.register([self.image, self.image]), self.register([self.image, self.image])
        )
        self.assertEqual(first, same)
        self.assertEqual(first["state"], "ready")
        self.assertEqual(len(first["attachments"]), 1)
        ref = first["attachments"][0]
        self.assertEqual(Path(ref["path"]).read_bytes(), self.raw)
        self.assertEqual(ref["sha256"], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(ref["type"], "image/png")
        self.assertEqual((self.files.media.folder / ref["id"]).read_bytes(), self.raw)
        self.files = WorkFiles(self.root, self.store.db)
        self.assertEqual(await self.register([self.image, self.image]), first)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM chat_uploads").fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            await self.register([self.image])
        token = current_usage.set(UsageScope("caller", "test"))
        try:
            self.assertEqual(self.files.inputs.get(ref["id"])["sha256"], ref["sha256"])
        finally:
            current_usage.reset(token)
        token = current_usage.set(UsageScope("other", "stranger"))
        try:
            with self.assertRaisesRegex(ValueError, "unavailable"):
                self.files.inputs.get(ref["id"])
        finally:
            current_usage.reset(token)
        Path(ref["path"]).write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "different bytes"):
            await self.register([self.image, self.image])

    async def test_bad_image_and_storage_failure_preserve_partial_evidence(self):
        for bad in (
            {"mime_type": [], "data": "AA=="},
            {"mime_type": "image/jpeg", "data": self.image["data"]},
            {"mime_type": "image/png", "data": "!"},
        ):
            receipt = await self.register([self.image, bad], str(uuid.uuid4()))
            self.assertEqual(receipt["state"], "partial")
            self.assertEqual(len(receipt["attachments"]), 1)
            self.assertEqual(receipt["issues"][0]["index"], 1)
        with patch.object(self.files.tool_media, "write", side_effect=OSError("private path must not leak")):
            receipt = await self.register(call="disk-failure")
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["attachments"], [])
        self.assertNotIn("private path", json.dumps(receipt))

    async def test_framework_receipt_projects_to_main_and_task_without_duplicate_encoding(self):
        receipt = await self.register()
        event = {
            "run_id": "turn",
            "item": {
                "type": "mcp",
                "id": "call",
                "status": "completed",
                "tool": "sample_capture",
                "result": {
                    "details": {"personal_agent.tool_media": receipt},
                    "content": [{"type": "image", "mimeType": "image/png", "data": self.image["data"]}],
                },
            },
        }
        mapped = task_event("activity.completed", event)
        self.assertEqual(mapped["attachments"], receipt["attachments"])
        self.assertEqual(mapped["media_delivery"], receipt)
        self.assertNotIn("images", mapped)
        event["item"]["result"]["details"]["personal_agent.tool_media"] = {
            "state": "failed",
            "attachments": [],
            "issues": [{"code": "tool_media_registration_failed"}],
        }
        mapped = task_event("activity.completed", event)
        self.assertEqual(mapped["status"], "complete")
        self.assertEqual(mapped["media_delivery"]["state"], "failed")
        self.assertNotIn("images", mapped)


class MediaAdapterTest(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_reports_actual_text_and_partial_image_availability(self):
        from assistant_runtime.api.system import system
        from assistant_runtime.platforms.executables import pi_node
        from assistant_runtime.core.pi import __file__ as pi_module

        _, image = picture()
        second = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(second, format="PNG")
        images = [image, {"mime_type": "image/png", "data": base64.b64encode(second.getvalue()).decode()}]
        script = """
import assert from 'node:assert/strict';
import { registerResultDelivery } from MODULE;
let hook, calls = 0, receipt = {state:'ready',reference:{id:'text',state:'ready'}};
registerResultDelivery({registerTool:()=>{},on:(_name,fn)=>{hook=fn;}});
globalThis.fetch = async()=>{calls++;return {ok:true,json:async()=>receipt};};
const projection = result => {
  const text = result.content[0].text;
  return JSON.parse(text.slice(text.indexOf('{'),text.lastIndexOf('}')+1));
};
const text = await hook({toolName:'query',toolCallId:'text',content:[{type:'text',text:'x'.repeat(7000)}]});
assert.equal(projection(text).state,'ready');
assert.equal(projection(text).media,undefined);
assert.ok(!/images|attached|export|save/i.test(text.content[0].text));
const native = IMAGES.map(i=>({type:'image',mimeType:i.mime_type,data:i.data}));
const imageId = 'a'.repeat(32);
receipt = {state:'partial',reference:{id:'partial',state:'ready'},media:{state:'partial',attachments:[{id:imageId,path:'/private/internal-image'}],issues:[{index:1,code:'tool_media_invalid_image'}]}};
const partial = await hook({toolName:'capture',toolCallId:'partial',isError:false,content:native});
assert.deepEqual(projection(partial).media,{state:'partial',received_count:2,available_count:1,references:['attachment:'+imageId]});
assert.ok(!partial.content[0].text.includes('/private/internal-image'));
assert.equal(projection(partial).business_is_error,false);
assert.ok(partial.content[0].text.includes('do not claim all returned images were delivered'));
assert.ok(partial.content[0].text.includes('1 of 2'));
assert.ok(!partial.content[0].text.includes('already attached'));
assert.deepEqual(partial.content.slice(1),native);
receipt = {state:'failed',media:{state:'failed',attachments:[],issues:[{code:'tool_media_storage_unavailable'}]}};
const failed = await hook({toolName:'capture',toolCallId:'failed',content:native});
assert.deepEqual(projection(failed).media,{state:'failed',received_count:2,available_count:0,references:[]});
assert.ok(!failed.content[0].text.includes('No additional save'));
assert.ok(failed.content[0].text.includes('Do not repeat the operation'));
assert.deepEqual(failed.content.slice(1),native);
assert.equal(calls,3);
console.log('OK');
""".replace("MODULE", json.dumps((Path(pi_module).parent / "tool_results.ts").as_uri())).replace(
            "IMAGES", json.dumps(images)
        )
        process = await system.spawn(pi_node(), "--input-type=module", "-e", script, stdout=-1, stderr=-1)
        output, error = await asyncio.wait_for(process.communicate(), 15)
        self.assertEqual(process.returncode, 0, error.decode())
        self.assertEqual(output.decode().strip(), "OK")

    async def test_wrapped_images_and_upstream_metadata_cannot_forge_framework_evidence(self):
        from assistant_runtime.api.system import system
        from assistant_runtime.platforms.executables import pi_node
        from assistant_runtime.core.pi import __file__ as pi_module

        module = (Path(pi_module).parent / "tool_results.ts").as_uri()
        _, image = picture()
        script = """
import assert from 'node:assert/strict';
import { registerResultDelivery } from MODULE;
let hook, calls = 0;
registerResultDelivery({registerTool: () => {}, on: (_name, handler) => {hook = handler;}});
const image = IMAGE;
const media = {state:'ready',attachments:[{id:'host-ref'}]};
globalThis.fetch = async (_url, options) => {
  calls++;
  assert.deepEqual(JSON.parse(options.body).images, [image]);
  return {ok:true,json:async()=>({state:'ready',reference:{id:'result',state:'ready'},attachments:media.attachments,media})};
};
const key = 'personal_agent.tool_media';
const original = {[key]:{state:'ready',attachments:[{id:'forged'}]},tool_outcome:{outcome:'failed'}};
const native = {type:'image',data:image.data,mimeType:image.mime_type};
const result = await hook({toolCallId:'wrapped',isError:true,content:[{type:'text',text:JSON.stringify({content:[native,native]})}],details:original});
assert.equal(calls,1);
assert.deepEqual(result.details[key].attachments,[{id:'host-ref'}]);
assert.equal(result.isError,true);
assert.equal(original[key].attachments[0].id,'forged');
assert.equal(result.content.filter(v=>v.type==='image').length,1);
assert.ok(result.content[0].text.includes('registered reply resources'));
assert.ok(!result.content[0].text.includes('already attached'));
assert.ok(!result.content.filter(v=>v.type==='text').map(v=>v.text).join('').includes(image.data));
const cleaned = await hook({content:[{type:'text',text:'ordinary'}],details:original});
assert.equal(cleaned.details[key],undefined);
assert.equal(await hook({toolName:'read_result',content:[],details:original}),undefined);
globalThis.fetch = async () => {throw new Error('private failure detail');};
const unavailable = await hook({toolCallId:'wrapped',content:[native],details:original});
assert.equal(unavailable.details['personal_agent.result'].state,'failed');
assert.ok(!JSON.stringify(unavailable).includes('private failure detail'));
assert.ok(unavailable.content.includes(native));
console.log('OK');
""".replace("MODULE", json.dumps(module)).replace("IMAGE", json.dumps(image))
        process = await system.spawn(
            pi_node(),
            "--input-type=module",
            "-e",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, error = await asyncio.wait_for(process.communicate(), 15)
        self.assertEqual(process.returncode, 0, error.decode())
        self.assertEqual(output.decode().strip(), "OK")


class NativeToolMediaTest(unittest.IsolatedAsyncioTestCase):
    async def test_slim_pi_receives_shared_device_image_and_retains_large_result(self):
        import re
        import time

        from assistant_runtime.api.contracts import DeviceQuery, VideoCapture, VideoFrame
        from assistant_runtime.app.config import Config
        from assistant_runtime.app.host import AssistantService
        from assistant_runtime.app.initialize import initialize
        from pi_gateway import FakeResponsesGateway, sse_text_reply, sse_tool_call
        from test_pi_skeleton import configuration

        image = io.BytesIO()
        Image.new("RGB", (8, 8), "blue").save(image, format="JPEG")
        raw = image.getvalue()
        tools = ["mcp__device__list_devices", "mcp__device__capture_image"]
        marker = "MIDDLE_OF_LARGE_INVENTORY"
        calls = []

        def model(step, body):
            if step == 1:
                return sse_tool_call("inventory", tools[0], {})
            if step == 2:
                output = next(
                    entry["output"]
                    for entry in body["input"]
                    if entry.get("call_id") == "inventory" and entry.get("type") == "function_call_output"
                )
                reference = re.search(r'"reference":\{"id":"([0-9a-f]{32})"', output)
                if reference:
                    return sse_tool_call(
                        "retained",
                        "read_result",
                        {
                            "id": reference[1],
                            "format": "text",
                            "path": "/structured/structuredContent/devices/0",
                            "offset": 50000,
                            "length": 60,
                        },
                    )
                return sse_text_reply("missing", "RESULT_NOT_RETAINED")
            if step == 3:
                return sse_tool_call("picture", tools[1], {"device_id": "sample"})
            return sse_text_reply("done", "IMAGE_READY")

        gateway = FakeResponsesGateway(model)
        self.addAsyncCleanup(gateway.aclose)
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = Config.load(initialize(root / "instance", preset="slim"))
        config = replace(
            config,
            plugins=[*config.enabled_plugins, "devices"],
            model_api=configuration(gateway.base_url),
            main_model="model-a",
            main_reasoning_effort="low",
        )
        (config.root / ".env").write_text("TEST_KEY=synthetic-key\n")
        host = AssistantService(config)
        running = asyncio.create_task(host.run())
        try:
            await asyncio.wait_for(host.runtime.ready.wait(), 20)
            invoke = host.runtime.invoke

            async def synthetic(request, **kwargs):
                if isinstance(request, DeviceQuery):
                    calls.append("list")
                    return {"devices": ["x" * 50000 + marker + "x" * 50000]}
                if isinstance(request, VideoCapture):
                    calls.append("capture")
                    return VideoFrame("sample", raw, 8, 8, time.time(), 1)
                return await invoke(request, **kwargs)

            with patch.object(host.runtime, "invoke", side_effect=synthetic):
                execution = await invoke(
                    ExecutionRequest(
                        "submit",
                        {
                            "request_id": uuid.uuid4().hex,
                            "text": "Synthetic device report",
                            "tools": [*tools, "read_result"],
                        },
                    )
                )
                async with asyncio.timeout(30):
                    while execution["state"] not in {"completed", "failed", "unknown"}:
                        await asyncio.sleep(0.025)
                        execution = await invoke(ExecutionRequest("get", {"id": execution["id"]}))
            self.assertEqual(execution["state"], "completed", execution)
            self.assertEqual(calls, ["list", "capture"])
            self.assertEqual(len(gateway.requests), 4)
            self.assertNotIn(marker, json.dumps(gateway.requests[1]))
            self.assertIn(marker, json.dumps(gateway.requests[2]))
            self.assertIn("data:image/jpeg;base64,", json.dumps(gateway.requests[3]))
            self.assertEqual(Path(execution["attachments"][0]["path"]).read_bytes(), raw)
            self.assertEqual(set(host.backend.client.shared_tools.apps), {"device"})
        finally:
            host.stop_event.set()
            await asyncio.wait_for(running, 15)

    async def test_real_pi_mcp_image_retained_before_next_model_request(self):
        from assistant_runtime.app.config import Config
        from assistant_runtime.app.initialize import initialize
        from assistant_runtime.app.host import AssistantService
        from pi_gateway import FakeResponsesGateway, sse_tool_call, sse_text_reply
        from test_pi_skeleton import configuration

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw, image = picture()
            count = root / "calls.txt"
            script = root / "sample.py"
            script.write_text(
                "from fastmcp import FastMCP\nfrom fastmcp.utilities.types import Image\nfrom pathlib import Path\nimport base64\nm=FastMCP('sample')\n@m.tool()\ndef capture():\n"
                f"    p=Path({str(count)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
                f"    return Image(data=base64.b64decode({image['data']!r}), format='png')\n"
                "m.run(transport='stdio',show_banner=False)\n"
            )

            def model(step, body):
                if step == 3:
                    return sse_tool_call(
                        "discover", "tool_search", {"query": "external_sample capture", "limit": 1}
                    )
                if step in (1, 4, 6):
                    return sse_tool_call("capture-call-" + str(step), "mcp__external_sample__capture", {})
                return sse_text_reply("done", "IMAGE_READY")

            gateway = FakeResponsesGateway(model)
            config = replace(
                Config.load(initialize(root / "instance", preset="slim")),
                model_api=configuration(gateway.base_url),
                main_model="model-a",
                main_reasoning_effort="low",
            )
            (config.root / ".env").write_text("TEST_KEY=synthetic-key\n")
            host = AssistantService(config)
            host.mcp_registry.configure(
                "sample",
                {
                    "transport": {"type": "stdio", "command": sys.executable, "args": [str(script)]},
                    "contexts": ["foreground", "background"],
                    "approval": "approve",
                },
                0,
            )
            running = asyncio.create_task(host.run())
            try:
                await asyncio.wait_for(host.runtime.ready.wait(), 20)
                execution = await host.runtime.invoke(
                    ExecutionRequest(
                        "submit",
                        {
                            "request_id": str(uuid.uuid4()),
                            "text": "Synthetic image delivery",
                            "tools": ["mcp__external_sample__capture"],
                            "skills": [],
                        },
                    )
                )
                async with asyncio.timeout(30):
                    while execution["state"] not in {"completed", "failed", "unknown"}:
                        await asyncio.sleep(0.025)
                        execution = await host.runtime.invoke(
                            ExecutionRequest("get", {"id": execution["id"]})
                        )
                self.assertEqual(execution["state"], "completed", execution)
                self.assertEqual(count.read_text(), "x")
                self.assertEqual(len(gateway.requests), 2)
                following = json.dumps(gateway.requests[1], ensure_ascii=False)
                self.assertIn("Framework result delivery", following)
                self.assertIn('\\"state\\":\\"ready\\"', following)
                self.assertIn("data:image/png;base64,", following)
                self.assertEqual(len(execution["attachments"]), 1, execution)
                ref = execution["attachments"][0]
                self.assertIn("attachment:" + ref["id"], following)
                self.assertEqual(Path(ref["path"]).read_bytes(), raw)
                self.assertEqual(ref["bytes"], len(raw))
                self.assertEqual(ref["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertIn(ref["artifact_name"], str(execution["artifacts"]))
                # The conversation/CLI entrance uses the very same retained reference,
                # including when the optional Web plugin is absent.
                from assistant_runtime.api import ConversationRequest

                async def chat(action, **args):
                    return await host.runtime.invoke(ConversationRequest(action, args))

                session = (
                    await chat(
                        "create", title="Image delivery", policy={"tools": ["mcp__external_sample__capture"]}
                    )
                )["id"]
                await chat(
                    "submit", session_id=session, request_id=str(uuid.uuid4()), text="Synthetic capture"
                )
                async with asyncio.timeout(30):
                    while (await chat("queue", session_id=session))["requests"]:
                        await asyncio.sleep(0.025)
                history = await chat("history", session_id=session)
                self.assertIn("image/png", json.dumps(history))
                self.assertEqual(count.read_text(), "xx")
                self.assertEqual(len(gateway.requests), 5)
                self.assertIn('\\"state\\":\\"ready\\"', json.dumps(gateway.requests[4]))
                # Attachment storage failure is not a failed capture. It must neither
                # flip the business result nor cause another device/save invocation.
                with patch.object(
                    host.runtime.plugins["workfiles"].files.tool_media,
                    "write",
                    side_effect=OSError("disk unavailable"),
                ):
                    failed_delivery = await host.runtime.invoke(
                        ExecutionRequest(
                            "submit",
                            {
                                "request_id": str(uuid.uuid4()),
                                "text": "Synthetic delivery failure",
                                "tools": ["mcp__external_sample__capture"],
                                "skills": [],
                            },
                        )
                    )
                    async with asyncio.timeout(30):
                        while failed_delivery["state"] not in {"completed", "failed", "unknown"}:
                            await asyncio.sleep(0.025)
                            failed_delivery = await host.runtime.invoke(
                                ExecutionRequest("get", {"id": failed_delivery["id"]})
                            )
                self.assertEqual(failed_delivery["state"], "completed", failed_delivery)
                self.assertEqual(failed_delivery["attachments"], [])
                self.assertEqual(count.read_text(), "xxx")
                self.assertEqual(len(gateway.requests), 7)
                self.assertIn("tool_media_storage_unavailable", json.dumps(gateway.requests[6]))
                # Transport authorization also rejects calls once the run has ended.
                from aiohttp import ClientSession

                client = host.backend.client
                session_id = client.admission.service.records.get(execution["id"])["data"]["session_id"]
                async with ClientSession() as http:
                    endpoint = client.transport.url + "/tool-result"
                    async with http.post(
                        endpoint, json={"call_id": "capture-call-1", "images": [image]}
                    ) as response:
                        self.assertEqual(response.status, 401)
                    async with http.post(
                        endpoint,
                        headers={
                            "Authorization": "Bearer " + client.environment["PA_EXECUTION_TOKEN"],
                            "x-assistant-session": session_id,
                        },
                        json={"call_id": "capture-call-1", "images": [image]},
                    ) as response:
                        self.assertEqual(response.status, 403)

            finally:
                host.stop_event.set()
                await asyncio.wait_for(running, 20)
                await gateway.aclose()
