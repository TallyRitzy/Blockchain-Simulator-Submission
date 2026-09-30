"""Deliberate attacks reuse the honest node's networking and validation."""
import base64
import copy
import uuid

from consensus.pos.p2p import Peer as HonestPeer
from consensus.pos.blockchain_structures import Transaction, sign_block


class Peer(HonestPeer):
    malicious = True

    async def publish_block(self, block):
        alternate = copy.deepcopy(block)
        alternate.id = str(uuid.uuid4())
        sign_block(alternate, self.wallet.private_key)
        await super().publish_block(block)
        self.record_event("attack", "Broadcast two different signed blocks for the same parent and slot")
        await self.broadcast_message({
            "type": "new_block", "id": str(uuid.uuid4()),
            "block": alternate.to_dict_with_stakers(),
            "sign": base64.b64encode(alternate.sign).decode(),
        })

    async def send_invalid_transaction(self, receiver):
        self.require_ready()
        tx = Transaction(self.chain.calc_balance(self.wallet.public_key_pem) + 1000,
                         self.wallet.public_key_pem, receiver, network=self.network_id)
        tx.sign = self.wallet.private_key.sign_deterministic(str(tx).encode())
        await self.broadcast_message({
            "type": "new_tx", "id": str(uuid.uuid4()), "transaction": str(tx),
            "sign": base64.b64encode(tx.sign).decode(), "sender_pem": tx.sender,
        })
        self.record_event("attack", "Sent a deliberately unaffordable transaction")
        return tx.id