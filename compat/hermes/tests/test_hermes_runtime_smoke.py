import json
import threading
import unittest
from urllib.request import Request, urlopen

from compat.hermes.hermes_runtime_smoke import fixture_server


class OfflineHermesFixtureTests(unittest.TestCase):
    def test_fixture_serves_json_and_terminal_sse_then_stops_worker(self):
        workers_before = {thread.ident for thread in threading.enumerate() if thread.name == "hermes-compat-http"}
        with fixture_server() as (url, requests):
            payload = {"model": "hermes-compat-fixture", "messages": [{"role": "user", "content": "fixture"}]}
            for streaming in (False, True):
                body = json.dumps({**payload, "stream": streaming}).encode()
                request = Request(url + "/chat/completions", data=body, headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=5) as response:
                    raw = response.read()
                    if streaming:
                        self.assertEqual(response.headers["Content-Type"], "text/event-stream")
                        frames = [line[6:] for line in raw.decode().splitlines() if line.startswith("data: ")]
                        self.assertEqual(frames[-1], "[DONE]")
                        self.assertEqual(json.loads(frames[-2])["choices"][0]["finish_reason"], "stop")
                    else:
                        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "hermes-sdk-fixture-ok")
            self.assertEqual([item["stream"] for item in requests], [False, True])
        workers_after = {thread.ident for thread in threading.enumerate() if thread.name == "hermes-compat-http"}
        self.assertEqual(workers_after, workers_before)
