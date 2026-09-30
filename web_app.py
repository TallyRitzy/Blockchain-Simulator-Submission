"""Local browser controls; every action is dispatched to the node's asyncio loop."""
import asyncio
import concurrent.futures
import secrets
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from werkzeug.serving import make_server
from consensus.pos.blockchain_structures import valid_public_key

ROOT = Path(__file__).resolve().parent


def cosmo_polo_telemetry(state):
    return "Mission Control Status: Stellar" if state["ready"] else "Synchronising"


def create_app(peer, loop, port, token):
    app = Flask(__name__, template_folder=str(ROOT / "web/templates"),
                static_folder=str(ROOT / "web/static"), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    def call(coroutine, timeout=30):
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError("The node is busy; try again shortly")

    @app.before_request
    def check_request():
        # Mission console stays local and requires a token for commands.
        if request.host not in allowed_hosts:
            return jsonify(error="Unrecognised host"), 403
        if request.method == "POST":
            if not request.is_json or request.headers.get("X-Simulation-Token") != token:
                return jsonify(error="Missing command token or JSON body"), 403
            origin = request.headers.get("Origin")
            if origin and origin not in {f"http://{host}" for host in allowed_hosts}:
                return jsonify(error="Different browser origin"), 403

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        return response

    @app.errorhandler(ValueError)
    @app.errorhandler(TypeError)
    @app.errorhandler(KeyError)
    def bad_request(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(TimeoutError)
    def busy(error):
        return jsonify(error=str(error)), 503

    @app.get("/")
    def index():
        return render_template("index.html", token=token)

    @app.get("/api/state")
    def state():
        result = call(peer.snapshot(), timeout=5)
        result["telemetry"] = cosmo_polo_telemetry(result)
        return jsonify(result)

    def body():
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        return data

    @app.post("/api/transactions")
    def transaction():
        data = body()
        receiver = data.get("receiver")
        if not valid_public_key(receiver):
            raise ValueError("Choose a peer or provide a valid public key")
        return jsonify(id=call(peer.create_and_broadcast_tx(receiver, data.get("amount"))))

    @app.post("/api/stakes")
    def stake():
        return jsonify(id=call(peer.send_stake_announcements(body().get("amount"))))

    @app.post("/api/contracts/deploy")
    def deploy():
        code = body().get("code")
        if not isinstance(code, str) or not 0 < len(code) <= 16000:
            raise ValueError("Contract code must contain 1-16000 characters")
        fee = round((len(code) // 10 + 5) * 0.001, 6)
        return jsonify(id=call(peer.create_and_broadcast_tx("deploy", [code, fee])))

    @app.post("/api/contracts/invoke")
    def invoke():
        data = body()
        cid, name, args = data.get("contract_id"), data.get("function"), data.get("args")
        if not isinstance(cid, str) or not isinstance(name, str) or not isinstance(args, list):
            raise ValueError("Provide a contract ID, function name and JSON array of arguments")

        async def submit():
            peer.require_ready()
            result = await asyncio.to_thread(peer.run_contract, [cid, name, args])
            if not result["success"]:
                raise ValueError(result["error"])
            payload = [cid, name, args, result["state"], round(result["gas_used"] * 0.001, 6)]
            return await peer.create_and_broadcast_tx("invoke", payload)
        return jsonify(id=call(submit(), timeout=45))

    @app.post("/api/attack")
    def attack():
        data = body()
        if not getattr(peer, "malicious", False):
            return jsonify(error="Start this node with --malicious to run a deliberate attack"), 403
        if not valid_public_key(data.get("receiver")):
            raise ValueError("Choose a receiver")
        return jsonify(id=call(peer.send_invalid_transaction(data["receiver"])))

    return app


class NodeWebServer:
    def __init__(self, peer, loop, port):
        if not 1 <= port <= 65535:
            raise ValueError("Invalid web port")
        self.token = secrets.token_urlsafe(32)
        self.app = create_app(peer, loop, port, self.token)
        self.server = make_server("127.0.0.1", port, self.app, threaded=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

