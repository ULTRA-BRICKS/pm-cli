"""A fake task-cli 0.23.1 Laya provider that speaks the REAL wire shapes.

Accessed via: the classifier, bench, collector and realistic-regression tests. The reply shapes
below are copied from the live provider (api_version 2): ``meta`` is a TOP-LEVEL key of the reply,
beside ``result``; ``result.usage`` carries the server's own truncation accounting; and a request
without a ``model`` field is answered by the ENGLISH checkpoint. Nothing here is a private chat.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

CHECKPOINTS = {
    "english": "convaiinnovations/laya",
    "multilingual": "convaiinnovations/laya-multilingual",
}
GOOD_ANSWERS = {
    "task_finished": {"type": "noul", "noul": 0.91},
    "steps_remaining": {"type": "noul", "noul": 0.04},
    "blocked_on_human": {"type": "noul", "noul": 0.02},
    "response_finished": {"type": "noul", "noul": 0.97},
}


def capabilities_reply(
    *, english_loaded: bool = True, multilingual_loaded: bool = True, **overrides
) -> dict:
    """The live ``op: capabilities`` reply; ``overrides`` replace top-level ``result`` keys."""
    result = {
        "api_version": 2,
        "default_model": "english",
        "models": {
            "english": {
                "checkpoint": CHECKPOINTS["english"],
                "loaded": english_loaded,
                "max_len": {"min": 512, "default": 512, "max": 2048},
            },
            "multilingual": {
                "checkpoint": CHECKPOINTS["multilingual"],
                "loaded": multilingual_loaded,
                "max_len": {"min": 512, "default": 1024, "max": 2048},
            },
        },
    }
    result.update(overrides)
    return {"ok": True, "result": result}


def predict_reply(
    answers: dict | None = None,
    *,
    route: str = "multilingual",
    max_len: int = 2048,
    truncated: bool = False,
    dropped: int = 0,
    meta: dict | None = None,
    usage: dict | None = None,
) -> dict:
    """The live routed ``predict`` reply: ``meta`` beside ``result``, never inside it."""
    reply = {
        "ok": True,
        "result": {
            "model": "laya-rl-agent",
            "answers": GOOD_ANSWERS if answers is None else answers,
            "usage": {"truncated": truncated, "state_tokens_dropped": dropped}
            if usage is None
            else usage,
        },
        "meta": {
            "api_version": 2,
            "model": route,
            "checkpoint": CHECKPOINTS[route],
            "max_len": max_len,
            "truncated": truncated,
            "input_tokens": 410,
            "state_tokens": 380,
            "state_tokens_dropped": dropped,
        },
    }
    if meta is not None:
        reply["meta"] = meta
    return reply


def over_length_reply() -> dict:
    """What the provider really returned for a ~9000-token request at max_len 2048."""
    reply = predict_reply(route="multilingual", max_len=2048, truncated=True, dropped=8520)
    reply["meta"]["input_tokens"] = 9001
    reply["meta"]["state_tokens"] = 2048
    return reply


def encode(reply: dict) -> bytes:
    return json.dumps(reply).encode() + b"\n"


class FakeLaya:
    """A unix-socket server in a private tmp dir that answers like provider 0.23.1.

    ``requests`` are the ``predict`` request lines; ``capability_requests`` counts capabilities
    reads. ``reply`` (bytes) overrides every predict answer; ``capabilities`` (bytes) the
    capabilities answer; ``responder`` computes a predict reply from the parsed request.
    """

    def __init__(self, directory: Path, name: str = "laya-0.23.1.sock") -> None:
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        self.path = directory / name
        self.requests: list[bytes] = []
        self.capability_requests = 0
        self.reply: bytes | None = None
        self.capabilities: bytes | None = encode(capabilities_reply())
        self.responder: Callable[[dict], bytes | None] = self._echo_route
        self.delay = 0.0
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self.server.listen(8)
        self.stop = False
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    @staticmethod
    def _echo_route(request: dict) -> bytes:
        route = request.get("model", "english")
        return encode(predict_reply(route=route, max_len=request.get("max_len", 512)))

    def serve(self) -> None:
        while not self.stop:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                try:
                    request = json.loads(data)
                except ValueError:
                    request = {}
                if request.get("op") == "capabilities":
                    self.capability_requests += 1
                    reply = self.capabilities
                else:
                    self.requests.append(data)
                    if self.delay:
                        time.sleep(self.delay)
                    reply = self.reply if self.reply is not None else self.responder(request)
                if reply is not None:
                    try:
                        conn.sendall(reply)
                    except OSError:
                        pass

    def close(self) -> None:
        self.stop = True
        self.server.close()
