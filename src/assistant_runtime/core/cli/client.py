"""Small terminal client over the public conversation API, with no model execution."""

import asyncio
import getpass
import json
import re
import signal
import uuid
from assistant_runtime.api.system import system

HELP = """/new [title]  /sessions  /switch ID  /history  /queue
/supplement text  /answer QUESTION_ID text  /cancel [REQUEST_ID]
/help  /exit
Ordinary messages queue for the next turn. Exit disconnects; work continues.
Ctrl+C clears input. Use /cancel to cancel an execution."""


class Chat:
    def __init__(self, request, *, output=print, operator=None, attachment_root=None):
        self.request, self.output = request, output
        self.operator = operator or getpass.getuser()
        self.attachment_root = attachment_root
        self.session = None
        self.cursor = 0
        self.rendered = {}
        self.lock = asyncio.Lock()

    async def call(self, action, **arguments):
        return await self.request(action, arguments)

    async def select(self, ident=None):
        data = await self.call("open", operator=self.operator, **({"session_id": ident} if ident else {}))
        self.session = data["id"]
        self.rendered.clear()
        self.output(f"Conversation: {self.session} ({data['title']})")
        await self.history()
        state = await self.call("state", session_id=self.session)
        if state.get("configuration_required"):
            self.output(
                "Model configuration required. Open this instance's configuration console; no model request was sent."
            )
        if state.get("question_id"):
            self.output(
                f"Pending question {state['question_id']}: {json.dumps(state.get('questions', []), ensure_ascii=False)}"
            )

    def message(self, item):
        ident, text = item["id"], item.get("text", "")
        saved = self.rendered.get(ident)
        previous = saved[0] if saved else None
        snapshot = (text, json.dumps(item.get("attachments", []), sort_keys=True), item.get("status"))
        if saved != snapshot:
            delta = text[len(previous) :] if previous and text.startswith(previous) else text
            if delta:
                label = "tool" if item.get("kind") == "detail" else item.get("role", "assistant")
                self.output(f"[{label}] {delta}")
            if item.get("status") in {"queued", "unknown", "interrupted", "cancelled"}:
                self.output(f"[{item['status']}] {ident}")
            if item.get("attachments") and (not saved or saved[1] != snapshot[1]):
                for attachment in item["attachments"]:
                    self.output("[attachment] " + json.dumps(attachment, ensure_ascii=False))
                    if self.attachment_root and re.fullmatch(r"[a-f0-9]{32}", attachment.get("id", "")):
                        self.output("Open local file: " + str(self.attachment_root / attachment["id"]))
            self.rendered[ident] = snapshot
            if len(self.rendered) > 200:
                del self.rendered[next(iter(self.rendered))]

    async def history(self):
        data = await self.call("history", session_id=self.session)
        for item in data["messages"]:
            self.message(item)
        self.cursor = data["cursor"]
        if data.get("before"):
            self.output("Showing the latest 60 messages.")

    async def poll(self):
        while True:
            await asyncio.sleep(0.2)
            async with self.lock:
                data = await self.call("events", session_id=self.session, cursor=self.cursor)
                if data["events"] is None:
                    await self.history()
                    continue
                for event in data["events"]:
                    value = event["data"]
                    if event["kind"] == "message" and value:
                        self.message(value)
                    elif event["kind"] == "question":
                        self.output(
                            f"[question {value['question_id']}] "
                            + json.dumps(value["questions"], ensure_ascii=False)
                        )
                    elif event["kind"] == "notice":
                        self.output("[notice] " + value.get("text", ""))
                self.cursor = data["cursor"]

    async def active(self):
        queue = await self.call("queue", session_id=self.session)
        active = [r for r in queue["requests"] if r["state"] == "running"]
        if not active:
            raise ValueError("No running turn in this conversation")
        return active[0]["id"]

    async def command(self, line):
        line = line.strip()
        if not line:
            return True
        if not line.startswith("/"):
            ident = str(uuid.uuid4())
            data = await self.call("submit", session_id=self.session, request_id=ident, text=line)
            self.output(f"[{data['state']}] {ident}")
            return True
        command, _, argument = line.partition(" ")
        argument = argument.strip()
        if command == "/exit":
            return False
        if command == "/help":
            self.output(HELP)
        elif command == "/new":
            data = await self.call("create", title=argument or "CLI")
            await self.select(data["id"])
        elif command == "/sessions":
            before = None
            while True:
                data = await self.call("list", **({"before": before} if before else {}))
                for item in data["sessions"]:
                    self.output(f"{item['id']}  {item['title']}")
                before = data["before"]
                if not before:
                    break
        elif command == "/switch":
            if not argument:
                raise ValueError("Usage: /switch SESSION_ID")
            await self.select(argument)
        elif command == "/history":
            self.rendered.clear()
            await self.history()
        elif command == "/queue":
            self.output(json.dumps(await self.call("queue", session_id=self.session), ensure_ascii=False))
        elif command == "/cancel":
            result = await self.call(
                "cancel", session_id=self.session, **({"request_id": argument} if argument else {})
            )
            self.output(json.dumps(result, ensure_ascii=False))
        elif command == "/supplement":
            self.output(
                await self.call(
                    "supplement", session_id=self.session, request_id=await self.active(), text=argument
                )
            )
        elif command == "/answer":
            question, _, text = argument.partition(" ")
            self.output(
                await self.call(
                    "answer",
                    session_id=self.session,
                    request_id=await self.active(),
                    question_id=question,
                    text=text,
                )
            )
        else:
            raise ValueError("Unknown command; use /help")
        return True

    async def run(self, session=None, *, read=None):
        await self.select(session)
        self.output(HELP)
        poll = asyncio.create_task(self.poll())
        input_task = None
        previous = signal.getsignal(signal.SIGINT)

        def interrupt(*_):
            if input_task and not input_task.done():
                input_task.cancel()

        signal.signal(signal.SIGINT, interrupt)
        try:
            while True:
                self.output("> ")
                input_task = asyncio.create_task(read() if read else system.terminal_input(secret=False))
                done, _ = await asyncio.wait((input_task, poll), return_when=asyncio.FIRST_COMPLETED)
                if poll in done:
                    await poll
                try:
                    line = await input_task
                except asyncio.CancelledError:
                    self.output("Input cleared; execution continues. Use /cancel to cancel.")
                    continue
                except EOFError:
                    break
                try:
                    async with self.lock:
                        if not await self.command(line):
                            break
                except (ValueError, RuntimeError) as exc:
                    self.output(str(exc))
        finally:
            signal.signal(signal.SIGINT, previous)
            if input_task:
                input_task.cancel()
            poll.cancel()
            await asyncio.gather(poll, *([input_task] if input_task else []), return_exceptions=True)
            self.output("Disconnected. Accepted work continues in the background.")
