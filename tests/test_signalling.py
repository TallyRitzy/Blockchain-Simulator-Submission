import asyncio
import base64
import hashlib
import json
import unittest

import websockets
from consensus.pos.blockchain_structures import Wallet, canonical
from signalling_server import SignallingServer


class SignallingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = SignallingServer()
        self.server = await websockets.serve(self.directory.handle, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        self.server.close()
        await self.server.wait_closed()

    async def register(self, room="room", create=True, network="a"*64, port=5000, name="Alice", forge=False):
        ws = await websockets.connect(self.url)
        self.clients.append(ws)
        nonce = json.loads(await ws.recv())["nonce"]
        wallet = Wallet()
        data = {"nonce": nonce, "room": room, "host": "127.0.0.1", "port": port,
                "name": name, "public_key": wallet.public_key_pem, "network": network,
                "create": create, "protocol": "pos-v2"}
        signature = wallet.private_key.sign_deterministic(canonical(data).encode(), hashfunc=hashlib.sha256)
        if forge:
            signature = b"forged"
        await ws.send(json.dumps({"registration": data, "sign": base64.b64encode(signature).decode()}))
        return ws, json.loads(await ws.recv())

    async def test_create_join_and_discover(self):
        _, first = await self.register()
        self.assertEqual(first["type"], "registered")
        _, joined = await self.register(create=False, network="", port=5001, name="Bob")
        self.assertEqual({p["name"] for p in joined["peers"]}, {"Alice", "Bob"})
        self.assertEqual(joined["network"], "a"*64)

    async def test_rooms_are_isolated(self):
        await self.register(room="one")
        _, other = await self.register(room="two", network="b"*64, port=5001, name="Bob")
        self.assertEqual([p["name"] for p in other["peers"]], ["Bob"])

    async def test_missing_room_rejected(self):
        _, result = await self.register(create=False, network="")
        self.assertEqual(result["type"], "error")

    async def test_wrong_genesis_and_duplicate_endpoint_rejected(self):
        await self.register()
        _, wrong = await self.register(network="b"*64, port=5001, name="Bob")
        self.assertEqual(wrong["type"], "error")
        _, duplicate = await self.register(name="Bob")
        self.assertEqual(duplicate["type"], "error")

    async def test_forged_identity_rejected(self):
        _, result = await self.register(forge=True)
        self.assertEqual(result["type"], "error")

    async def test_departed_peer_is_removed(self):
        ws, _ = await self.register()
        await ws.close()
        for _ in range(50):
            if not self.directory.rooms["room"]["members"]:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(self.directory.rooms["room"]["members"])

    async def test_invalid_room_id_rejected(self):
        _, result = await self.register(room="../elsewhere")
        self.assertEqual(result["type"], "error")


if __name__ == "__main__":
    unittest.main()

