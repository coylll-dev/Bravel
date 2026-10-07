from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread


class BridgeTests(unittest.TestCase):
    def test_real_stdio_protocol_preview_execution_and_followup(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                request = json.loads(payload["messages"][1]["content"])
                requests.append(request)
                command = "Write-Output 'bridge-observed'" if os.name == "nt" else "echo bridge-observed"
                plan = {"summary": "Готово" if len(requests) > 1 else "Проверка", "steps": [] if len(requests) > 1 else [{"command": command, "explanation": "test", "risk": "low"}]}
                body = json.dumps({"choices": [{"message": {"content": json.dumps(plan)}}]}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                environment = os.environ.copy()
                environment.update(BRAVEL_ENV=str(Path(directory) / "unused.env"), AI_PROVIDER="compatible",
                                   AI_API_BASE_URL=f"http://127.0.0.1:{server.server_port}/v1", AI_MODEL="fixture",
                                   AI_API_KEY="bridge-test-secret", AI_REQUIRE_KEY="true")
                process = subprocess.Popen([sys.executable, "-u", "-m", "bravel.bridge"], stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=environment)
                responses = queue.Queue()
                reader = Thread(target=lambda: [responses.put(line) for line in process.stdout], daemon=True)
                reader.start()
                next_id = 0

                def call(method, params=None):
                    nonlocal next_id
                    next_id += 1
                    process.stdin.write(json.dumps({"id": next_id, "method": method, "params": params or {}}) + "\n")
                    process.stdin.flush()
                    raw = responses.get(timeout=10)
                    self.assertNotIn("bridge-test-secret", raw)
                    message = json.loads(raw)
                    self.assertEqual(message["id"], next_id)
                    return message

                try:
                    self.assertEqual(call("status")["result"]["provider"], "compatible")
                    plan = call("plan", {"prompt": "test"})["result"]
                    self.assertEqual(len(plan["steps"]), 1)
                    self.assertIsNotNone(call("execute", {"plan_id": plan["plan_id"], "approval": ""})["error"])
                    result = call("execute", {"plan_id": plan["plan_id"], "approval": "approve"})["result"]
                    self.assertIn("bridge-observed", result["results"][0]["output"])
                    self.assertIsNotNone(call("execute", {"plan_id": plan["plan_id"], "approval": "approve"})["error"])
                    self.assertEqual(call("plan", {"continuation": True})["result"]["steps"], [])
                    self.assertIn("bridge-observed", json.dumps(requests[-1]["context"]["history"]))
                    self.assertIsNone(call("reset")["error"])
                finally:
                    process.stdin.close()
                    try:
                        process.wait(10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    process.stdout.close()
                    process.stderr.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
