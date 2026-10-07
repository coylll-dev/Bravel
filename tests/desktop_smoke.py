"""Exercise the real C# client and Python agent against a local fixture API."""
import json
import os
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

requests = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        request = json.loads(body["messages"][1]["content"])
        requests.append(request)
        command = "Write-Output 'desktop-observed'" if os.name == "nt" else "echo desktop-observed"
        steps = [] if len(requests) > 1 else [{"command": command, "explanation": "fixture", "risk": "low"}]
        plan = {"summary": "fixture", "steps": steps}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"choices": [{"message": {"content": json.dumps(plan)}}]}).encode())


server = HTTPServer(("127.0.0.1", 0), Handler)
thread = Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    with tempfile.TemporaryDirectory() as directory:
        environment = os.environ.copy()
        environment.update(BRAVEL_PYTHON=sys.executable, BRAVEL_TEST_MODE="1", BRAVEL_ENV=str(Path(directory) / ".env"),
                           AI_PROVIDER="compatible", AI_API_BASE_URL=f"http://127.0.0.1:{server.server_port}/v1",
                           AI_API_KEY="desktop-test-secret", AI_MODEL="fixture", AI_REQUIRE_KEY="true")
        dotnet = sys.argv[1] if len(sys.argv) > 1 else "dotnet"
        subprocess.run([dotnet, "desktop/Bravel.Desktop/bin/Release/net10.0/Bravel.Desktop.dll", "--check-backend"],
                       env=environment, check=True, timeout=45)
        assert "desktop-observed" in json.dumps(requests[-1]["context"]["history"])
finally:
    server.shutdown()
    server.server_close()
    thread.join()
