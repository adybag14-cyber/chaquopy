"""Real Hermes persistence and agent-loop checks against an offline HTTP fixture.

No actual model inference or sandbox agency is claimed. The same module runs on
the host for triage and in the experimental Hermes APK for Android evidence.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib
import json
import logging
import os
from pathlib import Path
import platform
import sys
import tempfile
import threading
import time
import uuid


@contextmanager
def fixture_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if self.path != "/v1/chat/completions" or not 0 < length < 1024 * 1024:
                self.send_error(400)
                return
            request = json.loads(self.rfile.read(length))
            requests.append(request)
            completion = {
                "id": "chatcmpl-hermes-compat", "object": "chat.completion", "created": 1,
                "model": "hermes-compat-fixture",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hermes-sdk-fixture-ok"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
            }
            if request.get("stream"):
                chunks = [
                    {"index": 0, "delta": {"role": "assistant", "content": "hermes-sdk-fixture-ok"}, "finish_reason": None},
                    {"index": 0, "delta": {}, "finish_reason": "stop"},
                ]
                payload = b"".join(
                    ("data: " + json.dumps({"id": completion["id"], "object": "chat.completion.chunk",
                                           "created": 1, "model": completion["model"], "choices": [chunk]}) + "\n\n").encode()
                    for chunk in chunks
                ) + b"data: [DONE]\n\n"
                content_type = "text/event-stream"
            else:
                payload = json.dumps(completion).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 5
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, name="hermes-compat-http")
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)
        if worker.is_alive():
            raise RuntimeError("Compatibility fixture HTTP worker did not stop")


def run(output_directory, require_android=False):
    if require_android and sys.platform != "android":
        raise RuntimeError("Android execution required; host runs are not accepted")
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    from run_agent import AIAgent

    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    checks = []
    for name in ("hermes_cli.config", "hermes_android.bootstrap", "hermes_android.server", "model_tools"):
        module = importlib.import_module(name)
        if not getattr(module, "__file__", None):
            raise AssertionError("Missing real module provenance: " + name)
        checks.append("import:" + name)
    session_id = "hermes-python-compat-" + uuid.uuid4().hex
    db = SessionDB(output_directory / "compatibility.db")
    try:
        db.create_session(session_id, "api_server", model="hermes-compat-fixture")
        db.append_message(session_id, "user", "compatibility fixture")
        if db.get_session(session_id) is None:
            raise AssertionError("Hermes session persistence failed")
        checks.append("hermes-session-persistence")
        with fixture_server() as (base_url, requests):
            agent = AIAgent(
                base_url=base_url, api_key="hermes-offline-fixture-not-a-real-key",
                provider="custom", api_mode="chat_completions", model="hermes-compat-fixture",
                max_iterations=2, max_tokens=64, enabled_toolsets=[], save_trajectories=False,
                quiet_mode=True, platform="api_server", skip_context_files=True, skip_memory=True,
                skip_background_review=True, run_budget_seconds=30, session_db=db,
                session_id=session_id, capabilities={"tools": False},
            )
            try:
                first = agent.run_conversation("Return the offline fixture marker.")
                if first.get("final_response") != "hermes-sdk-fixture-ok":
                    raise AssertionError("Real Hermes agent did not consume the SDK completion")
                second = agent.run_conversation("Return the marker again.", conversation_history=first["messages"])
                # The real runtime may also request a session title. Count the
                # two user turns independently of that legitimate auxiliary call.
                # This probe deliberately exercises streaming primary chat; the
                # title-generation endpoint uses the non-streaming JSON branch.
                primary_requests = [item for item in requests if item.get("stream") is True]
                if second.get("final_response") != "hermes-sdk-fixture-ok" or len(primary_requests) != 2:
                    raise AssertionError("Real Hermes multi-turn completion failed: " + json.dumps({
                        "final_response": second.get("final_response"), "error": second.get("error"),
                        "request_count": len(requests),
                        "requests": [{"model": item.get("model"), "stream": item.get("stream"),
                                      "roles": [message["role"] for message in item.get("messages", [])]}
                                     for item in requests],
                    }))
                first_system = [message for message in primary_requests[0]["messages"] if message["role"] == "system"]
                second_system = [message for message in primary_requests[1]["messages"] if message["role"] == "system"]
                if first_system != second_system:
                    raise AssertionError("System prompt changed between fixture turns")
                checks.append("hermes-real-agent-two-turn-http")
            finally:
                agent.close()
        checks.append("http-worker-shutdown")
    finally:
        db.close()
    report = {
        "schema": "hermes-python-consumer-runtime-v1", "status": "passed", "checks": checks,
        "python": platform.python_version(), "platform": sys.platform,
        "android_execution_verified": sys.platform == "android", "machine": platform.machine(),
        "hermes_home": str(get_hermes_home()), "model_inference_verified": False,
        "network_scope": "offline-loopback-only", "timestamp_unix": int(time.time()),
    }
    (output_directory / "hermes-runtime-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return json.dumps(report)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", type=Path, required=True)
    args = parser.parse_args()
    for key in tuple(os.environ):
        if key.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")) or key.startswith("HERMES_"):
            os.environ.pop(key, None)
    with tempfile.TemporaryDirectory(prefix="hermes-consumer-smoke-") as temporary:
        os.environ["HERMES_HOME"] = temporary
        os.environ["HERMES_EPHEMERAL"] = "1"
        sys.path.insert(0, str(args.hermes_root.resolve()))
        try:
            print(run(Path(temporary) / "evidence"))
        finally:
            logging.shutdown()
