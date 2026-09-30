"""Room discovery over WebSockets; blockchain traffic stays peer to peer."""
import argparse
import asyncio
import base64
import hashlib
import json
import re
import secrets
import socket
import uuid

import websockets
from ecdsa import VerifyingKey
from consensus.pos.blockchain_structures import canonical, valid_public_key

ROOM_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,40}")
MAX_ROOMS = 100
MAX_MEMBERS = 128


class SignallingServer:
    def __init__(self):
        self.rooms = {}

    def directory(self, room_id):
        room = self.rooms[room_id]
        return {"type": "peers", "room": room_id, "network": room["network"],
                "peers": list(room["members"].values())}

    async def notify(self, room_id):
        room = self.rooms[room_id]
        data = json.dumps(self.directory(room_id))
        async def send(ws):
            try:
                await asyncio.wait_for(ws.send(data), 2)
            except Exception:
                await ws.close()
        await asyncio.gather(*(send(ws) for ws in list(room["members"])))

    async def handle(self, ws):
        room_id = None
        try:
            nonce = secrets.token_hex(32)
            await ws.send(json.dumps({"type": "challenge", "nonce": nonce}))
            packet = json.loads(await asyncio.wait_for(ws.recv(), 10))
            data = packet["registration"]
            required = {"nonce", "room", "host", "port", "name", "public_key", "network", "create", "protocol"}
            if not isinstance(data, dict) or set(data) != required or data["nonce"] != nonce:
                raise ValueError("Invalid registration")
            if data["protocol"] != "pos-v2":
                raise ValueError("This signalling service supports PoS v2")
            candidate = data["room"]
            if not isinstance(candidate, str) or not ROOM_PATTERN.fullmatch(candidate):
                raise ValueError("Room ID must contain 1-40 letters, digits, underscores or hyphens")
            if (type(data["port"]) is not int or not 1 <= data["port"] <= 65535 or
                    not isinstance(data["host"], str) or len(data["host"]) > 253 or
                    not isinstance(data["name"], str) or not 0 < len(data["name"]) <= 40 or
                    not valid_public_key(data["public_key"]) or type(data["create"]) is not bool):
                raise ValueError("Invalid node details")
            VerifyingKey.from_pem(data["public_key"]).verify(
                base64.b64decode(packet["sign"], validate=True), canonical(data).encode(),
                hashfunc=hashlib.sha256)
            endpoint = (socket.gethostbyname(data["host"]), data["port"])
            if candidate not in self.rooms:
                if not data["create"]:
                    raise ValueError("Room does not exist; start one node with --create-room")
                if len(self.rooms) >= MAX_ROOMS or not re.fullmatch(r"[0-9a-f]{64}", data["network"]):
                    raise ValueError("Room capacity reached or missing genesis identity")
                self.rooms[candidate] = {"network": data["network"], "members": {}}
            room = self.rooms[candidate]
            if data["network"] and data["network"] != room["network"]:
                raise ValueError("Saved chain or newly created chain belongs to a different network")
            if len(room["members"]) >= MAX_MEMBERS:
                raise ValueError("Room is full")
            if not room["members"] and not data["create"] and not data["network"]:
                raise ValueError("No node is online to supply this room's history; restart a saved node")
            for other in room["members"].values():
                if (other["host"], other["port"]) == endpoint:
                    raise ValueError("That node address is already connected")
                if other["name"].lower() == data["name"].lower():
                    raise ValueError("Choose a unique node name within the room")
                if other["public_key"] == data["public_key"]:
                    raise ValueError("That wallet is already connected in this room")
            room_id = candidate
            room["members"][ws] = {"host": endpoint[0], "port": endpoint[1],
                                    "name": data["name"], "public_key": data["public_key"]}
            await ws.send(json.dumps(dict(self.directory(room_id), type="registered")))
            await self.notify(room_id)
            async for raw in ws:
                packet = json.loads(raw)
                if packet.get("type") == "peers":
                    await ws.send(json.dumps(self.directory(room_id)))
                else:
                    raise ValueError("Only peer discovery is supported here")
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as exc:
            try:
                await ws.send(json.dumps({"type": "error", "error": str(exc)[:200]}))
            except Exception:
                pass
        finally:
            if room_id is not None:
                self.rooms[room_id]["members"].pop(ws, None)
                await self.notify(room_id)
            await ws.close()


class SignallingClient:
    def __init__(self, peer, url, create_room):
        self.peer, self.url, self.create_room = peer, url, create_room
        self.ws = None

    def update(self, packet):
        if packet.get("type") == "error":
            raise ValueError(packet["error"])
        if packet.get("type") not in {"registered", "peers"} or packet.get("room") != self.peer.room_id:
            raise ValueError("Invalid discovery response")
        network = packet.get("network")
        if not isinstance(network, str) or not re.fullmatch(r"[0-9a-f]{64}", network):
            raise ValueError("Invalid room genesis identity")
        if self.peer.network_id and self.peer.network_id != network:
            raise ValueError("Room has a different genesis block")
        self.peer.network_id = network
        peers = packet.get("peers")
        if not isinstance(peers, list) or len(peers) > MAX_MEMBERS:
            raise ValueError("Invalid peer directory")
        for data in peers:
            self.peer.register_peer(data)

    async def connect(self):
        ws = await websockets.connect(self.url, max_size=2**18, open_timeout=5)
        try:
            challenge = json.loads(await asyncio.wait_for(ws.recv(), 5))
            if challenge.get("type") != "challenge":
                raise ValueError("Missing discovery challenge")
            data = {"nonce": challenge["nonce"], "room": self.peer.room_id, "host": self.peer.host,
                    "port": self.peer.port, "name": self.peer.name,
                    "public_key": self.peer.wallet.public_key_pem, "network": self.peer.network_id,
                    "create": self.create_room, "protocol": "pos-v2"}
            signature = self.peer.wallet.private_key.sign_deterministic(
                canonical(data).encode(), hashfunc=hashlib.sha256)
            await ws.send(json.dumps({"registration": data, "sign": base64.b64encode(signature).decode()}))
            self.update(json.loads(await asyncio.wait_for(ws.recv(), 5)))
            self.ws = ws
            self.peer.signalling_connected = True
            self.peer.record_event("discovery", "Connected to room directory")
        except BaseException:
            await ws.close()
            raise

    async def listen(self):
        delay = 1
        while not self.peer.stop_event.is_set():
            try:
                async for raw in self.ws:
                    self.update(json.loads(raw))
                raise ConnectionError("Discovery connection closed")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.peer.signalling_connected = False
                self.peer.record_event("discovery", "Directory offline; existing peer links remain active")
                await self.ws.close()
            while not self.peer.stop_event.is_set():
                await asyncio.sleep(delay)
                try:
                    await self.connect()
                    delay = 1
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    delay = min(delay * 2, 10)

    async def close(self):
        self.peer.signalling_connected = False
        if self.ws:
            await self.ws.close()


async def serve(host, port):
    server = SignallingServer()
    async with websockets.serve(server.handle, host, port, max_size=2**18):
        print(f"Signalling server: ws://{host}:{port}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Discover blockchain nodes by room ID")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.host, args.port))
    except KeyboardInterrupt:
        pass

