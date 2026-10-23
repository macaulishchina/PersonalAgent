import asyncio
import http.server
import json
import socketserver
import threading


class FakeResponsesGateway:
    """Local OpenAI-Responses SSE server for deterministic real `pi --mode rpc` tests."""

    def __init__(self, script_fn):
        self.script_fn = script_fn
        self.requests = []
        self.headers = []
        self.step = 0
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                parent.requests.append(body)
                parent.headers.append(dict(self.headers))
                parent.step += 1
                events = parent.script_fn(parent.step, body)
                if isinstance(events, tuple):
                    status, data = events
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(data).encode())
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for ev in events:
                    self.wfile.write(
                        f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")
                    )
                self.wfile.flush()

        self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    async def aclose(self):
        await asyncio.to_thread(self.close)


def sse_tool_call(call_id, name, args_dict):
    args_json = json.dumps(args_dict, ensure_ascii=False)
    return [
        {"type": "response.created", "response": {"id": f"resp_{call_id}", "status": "in_progress"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": f"fc_{call_id}",
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": "",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": f"fc_{call_id}",
            "output_index": 0,
            "delta": args_json,
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": f"fc_{call_id}",
            "output_index": 0,
            "arguments": args_json,
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": f"fc_{call_id}",
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": args_json,
                "status": "completed",
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": f"resp_{call_id}",
                "status": "completed",
                "output": [
                    {
                        "id": f"fc_{call_id}",
                        "type": "function_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": args_json,
                        "status": "completed",
                    }
                ],
                "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
            },
        },
    ]


def sse_text_reply(msg_id, text):
    return [
        {"type": "response.created", "response": {"id": f"resp_{msg_id}", "status": "in_progress"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
        },
        {
            "type": "response.content_part.added",
            "item_id": msg_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": ""},
        },
        {
            "type": "response.output_text.delta",
            "item_id": msg_id,
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        },
        {
            "type": "response.output_text.done",
            "item_id": msg_id,
            "output_index": 0,
            "content_index": 0,
            "text": text,
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": f"resp_{msg_id}",
                "status": "completed",
                "output": [
                    {
                        "id": msg_id,
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ],
                "usage": {"input_tokens": 25, "output_tokens": 12, "total_tokens": 37},
            },
        },
    ]
