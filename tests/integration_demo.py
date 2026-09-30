"""Exercise real processes, WebSockets, HTTP commands, malicious mode and restart."""
import argparse
import asyncio
import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]


def free_ports(count):
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [s.getsockname()[1] for s in sockets]
    finally:
        for sock in sockets:
            sock.close()


def wait_for(description, check, timeout=40):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = check()
            if last:
                print("PASS:", description, flush=True)
                return last
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.2)
    raise AssertionError(f"Timed out: {description}; last result: {last}")


def fetch(port, path="/api/state"):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
        return response.read().decode()


def state(port):
    return json.loads(fetch(port))


def post(port, path, payload):
    html = fetch(port, "/")
    token = re.search(r'name="simulation-token" content="([^"]+)"', html).group(1)
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", json.dumps(payload).encode(),
        {"Content-Type": "application/json", "X-Simulation-Token": token}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-running", action="store_true", help="Keep the demo alive for manual browser inspection")
    args = parser.parse_args()
    processes, streams = [], []
    with tempfile.TemporaryDirectory(prefix="blockchain-integration-") as directory:
        work = Path(directory)
        ports = free_ports(9)
        signal, a, wa, b, wb, m, wm, other, wo = ports
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
        def launch(label, argv):
            stream = open(work / f"{label}.log", "w", encoding="utf-8")
            streams.append(stream)
            process = subprocess.Popen([sys.executable, "-B", *argv], cwd=ROOT, env=env,
                stdout=stream, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            processes.append(process)
            return process
        def stop(process):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(5)
        def node(label, port, web, extra=(), room="integration"):
            return launch(label, ["start_peer.py", "--port", str(port), "--web-port", str(web),
                "--name", label.split("-")[0], "--room", room, "--signalling", f"ws://127.0.0.1:{signal}",
                "--epoch-seconds", "3", "--save", "--data-dir", str(work / "saved"), *extra])
        async def probe():
            async with websockets.connect(f"ws://127.0.0.1:{signal}", open_timeout=1) as ws:
                return json.loads(await ws.recv()).get("type") == "challenge"
        try:
            directory_process = launch("directory", ["signalling_server.py", "--port", str(signal)])
            wait_for("signalling server starts", lambda: asyncio.run(probe()))
            alice = node("Alice", a, wa, ["--create-room"])
            wait_for("creator has a valid genesis", lambda: state(wa)["ready"])
            bob = node("Bob", b, wb)
            malicious = node("Mallory", m, wm, ["--malicious"])
            wait_for("three nodes discover and synchronise", lambda:
                all(state(p)["ready"] and len(state(p)["peers"]) == 2 for p in (wa, wb, wm)))
            genesis = state(wa)["network"]
            assert all(state(p)["network"] == genesis for p in (wb, wm))
            bob_key, mal_key = state(wb)["public_key"], state(wm)["public_key"]
            post(wa, "/api/transactions", {"receiver": bob_key, "amount": 15})
            post(wa, "/api/transactions", {"receiver": mal_key, "amount": 15})
            wait_for("real transfers are confirmed", lambda: state(wb)["balance"] >= 15 and state(wm)["balance"] >= 15)
            post(wb, "/api/stakes", {"amount": 5})
            post(wm, "/api/stakes", {"amount": 5})
            wait_for("stake updates propagate through history", lambda:
                len(state(wa)["validators"]) == 3 and state(wb)["stake"] == 5)
            wait_for("double signing produces a confirmed penalty", lambda:
                state(wa)["confirmed_penalties"] > 0 and state(wm)["stake"] == 0, timeout=100)
            rejected = state(wa)["rejected_messages"]
            post(wm, "/api/attack", {"receiver": bob_key})
            wait_for("overspending attack is rejected", lambda: state(wa)["rejected_messages"] > rejected)
            expected_key = state(wb)["public_key"]
            stop(bob)
            time.sleep(0.4)
            bob = node("Bob-restarted", b, wb, ["--load"])
            wait_for("node restarts with its own saved wallet and valid history", lambda:
                state(wb)["ready"] and state(wb)["public_key"] == expected_key and state(wb)["confirmed_penalties"] > 0)
            assert state(wa)["public_key"] != state(wb)["public_key"]
            stop(directory_process)
            height = state(wa)["height"]
            wait_for("peer consensus continues with directory offline", lambda: state(wa)["height"] > height, timeout=30)
            directory_process = launch("directory-restarted", ["signalling_server.py", "--port", str(signal)])
            wait_for("directory reconnects automatically", lambda: all(state(p)["signalling_connected"] for p in (wa, wb, wm)), timeout=35)
            outsider = node("Dora", other, wo, ["--create-room"], room="isolated")
            wait_for("second room stays isolated", lambda: state(wo)["ready"] and state(wo)["network"] != genesis)
            assert state(wo)["peers"] == []
            assert len(state(wa)["peers"]) == 2
            wait_for("honest nodes converge on one tip", lambda:
                state(wa)["blocks"][-1]["hash"] == state(wb)["blocks"][-1]["hash"])
            code = 'def increment(amount, state):\n    state["count"] = state.get("count", 0) + amount\n    return state, "ok"\n'
            post(wa, "/api/contracts/deploy", {"code": code})
            wait_for("contract deployment reaches both honest nodes", lambda:
                state(wa)["contracts"] and state(wa)["contracts"] == state(wb)["contracts"], timeout=60)
            cid = state(wa)["contracts"][0]
            invoke = post(wa, "/api/contracts/invoke", {"contract_id": cid, "function": "increment", "args": [2]})["id"]
            def saved_invocation():
                histories = list((work / "saved").rglob("chain.json"))
                return sum(any(t["id"] == invoke for block in json.loads(path.read_text())
                               for t in block["transactions"]) for path in histories) >= 3
            wait_for("contract invocation is verified and saved by all three room nodes", saved_invocation, timeout=60)
            height = state(wa)["height"]
            for _ in range(15):
                assert state(wa)["ready"] and state(wb)["ready"]
                time.sleep(1)
            wait_for("dashboard stays responsive and blocks advance after contract execution",
                     lambda: state(wa)["height"] > height, timeout=30)
            print(json.dumps({"result": "PASS", "nodes": 4, "rooms": 2,
                              "browser_urls": [f"http://127.0.0.1:{p}" for p in (wa, wb, wm, wo)]}), flush=True)
            if args.keep_running:
                print("READY_FOR_BROWSER_REVIEW", flush=True)
                while True:
                    time.sleep(1)
        except BaseException:
            for stream in streams:
                stream.flush()
            for log in work.glob("*.log"):
                print(f"\n{log.name}\n{log.read_text(encoding='utf-8')[-5000:]}", flush=True)
            raise
        finally:
            for process in reversed(processes):
                stop(process)
            for stream in streams:
                stream.close()


if __name__ == "__main__":
    main()

