import asyncio
import base64
import copy
import hashlib
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from consensus.pos.blockchain_structures import (
    Block, Chain, Stake, Wallet, Transaction, amount_units, sign_block,
    registered_stakes, epoch_seed, lottery_wins, lottery_output,
    valid_block, valid_transaction, valid_evidence, isvalidChain,
    ledger_state, weight_of_chain, COIN_UNITS, EPOCH_TIME, chain_rank,
)
from consensus.pos.p2p import Peer
from shared_blockchain_structures import CommonChain, transaction_exists_in_block_list
from storage import storage_manager
from smart_contract.validation import validate_contract_transactions
from web_app import create_app


def fresh_chain():
    Chain.instance = None
    alice, bob = Wallet(), Wallet()
    chain = Chain(publicKey=alice.public_key_pem, privatekey=alice.private_key)
    genesis = chain.chain[0]
    genesis.ts -= 24 * 3600 * 1000
    genesis.transactions[0].ts = genesis.ts / 1000
    sign_block(genesis, alice.private_key)
    return chain, alice, bob


def transaction(chain, wallet, receiver, amount):
    tx = Transaction(amount, wallet.public_key_pem, receiver, network=chain.chain[0].hash)
    tx.sign = wallet.private_key.sign_deterministic(str(tx).encode())
    return tx


def candidate(prefix, wallet, txs=(), evidence=()):
    stakes = registered_stakes(prefix)
    registry = {s.staker: s.amt for s in stakes}
    slot = prefix[-1].slot + 1
    while slot < 1400:
        seed = epoch_seed(prefix, slot)
        if lottery_wins(wallet.public_key_pem, seed, registry.get(wallet.public_key_pem, 0), sum(registry.values())):
            break
        slot += 1
    if slot >= 1400:
        raise AssertionError("No eligible test slot")
    block = Block(prefix[-1].hash, list(txs), ts=prefix[0].ts + slot * EPOCH_TIME * 1000 + 100)
    block.creator = wallet.public_key_pem
    block.staked_amt = registry[block.creator]
    block.stakers = stakes
    block.slot, block.seed = slot, seed
    block.vrf_proof = wallet.private_key.sign_deterministic(seed.encode(), hashfunc=hashlib.sha256)
    block.slash_evidence = list(evidence)
    sign_block(block, wallet.private_key)
    return block


class ConsensusTests(unittest.TestCase):
    def setUp(self):
        self.chain, self.alice, self.bob = fresh_chain()

    def tearDown(self):
        Chain.instance = None

    def append(self, block):
        self.assertTrue(self.chain.isValidBlock(block))
        self.chain.chain.append(block)

    def activate_bob(self):
        fund = transaction(self.chain, self.alice, self.bob.public_key_pem, 15)
        self.append(candidate(self.chain.chain, self.alice, [fund]))
        stake = transaction(self.chain, self.bob, "stake", 5)
        self.append(candidate(self.chain.chain, self.alice, [stake]))

    def proof(self):
        one = candidate(self.chain.chain, self.alice)
        two = copy.deepcopy(one)
        two.id = "second-signed-proposal"
        sign_block(two, self.alice.private_key)
        return one, two, {"first": one.signed_header(), "second": two.signed_header()}

    def test_genesis_and_initial_locked_balance(self):
        self.assertTrue(isvalidChain(self.chain.chain))
        self.assertEqual(self.chain.calc_balance(self.alice.public_key_pem), 46)

    def test_single_staker_wins_and_history_agrees(self):
        block = candidate(self.chain.chain, self.alice)
        self.append(block)
        self.assertTrue(isvalidChain(self.chain.chain))

    def test_common_history_is_reused_but_new_suffix_is_verified(self):
        self.append(candidate(self.chain.chain, self.alice))
        incoming = copy.deepcopy(self.chain.chain)
        incoming[1].sign = b"untrusted-copy"
        verified = self.chain.validated_candidate(incoming)
        self.assertIs(verified[1], self.chain.chain[1])
        self.assertTrue(isvalidChain(verified))
        next_block = candidate(self.chain.chain, self.alice)
        next_block.staked_amt = 999
        sign_block(next_block, self.alice.private_key)
        self.assertIsNone(self.chain.validated_candidate(incoming + [next_block]))

    def test_losing_lottery_ticket_is_rejected(self):
        self.activate_bob()
        block = candidate(self.chain.chain, self.bob)
        while lottery_wins(block.creator, block.seed, 5, 15):
            block.slot += 1
            block.seed = epoch_seed(self.chain.chain, block.slot)
        block.ts = self.chain.chain[0].ts + block.slot * EPOCH_TIME * 1000
        block.vrf_proof = self.bob.private_key.sign_deterministic(block.seed.encode(), hashfunc=hashlib.sha256)
        sign_block(block, self.bob.private_key)
        self.assertFalse(self.chain.isValidBlock(block))

    def test_signature_retries_do_not_change_ticket(self):
        self.activate_bob()
        block = candidate(self.chain.chain, self.bob)
        for expected in (True, False):
            while lottery_wins(block.creator, block.seed, 5, 15) != expected:
                block.slot += 1
                block.seed = epoch_seed(self.chain.chain, block.slot)
            block.ts = self.chain.chain[0].ts + block.slot * EPOCH_TIME * 1000
            proofs = []
            for _ in range(3):
                block.vrf_proof = self.bob.private_key.sign(block.seed.encode(), hashfunc=hashlib.sha256)
                proofs.append(block.vrf_proof)
                sign_block(block, self.bob.private_key)
                self.assertEqual(self.chain.isValidBlock(block), expected)
            self.assertEqual(len(set(proofs)), 3)

    def test_stake_is_activated_only_after_confirmation(self):
        self.activate_bob()
        self.assertEqual({s.staker: s.amt for s in registered_stakes(self.chain.chain)}[self.bob.public_key_pem], 5)
        self.assertEqual(self.chain.calc_balance(self.bob.public_key_pem), 10)

    def test_stake_cannot_exceed_balance_or_withdraw(self):
        for amount in (57, 0, -1, 2.5, True, 9):
            tx = transaction(self.chain, self.alice, "stake", amount)
            self.assertFalse(valid_transaction(tx, self.chain.chain), amount)

    def test_only_one_pending_stake_update_per_sender(self):
        first = transaction(self.chain, self.alice, "stake", 11)
        second = transaction(self.chain, self.alice, "stake", 12)
        self.assertTrue(valid_transaction(first, self.chain.chain))
        self.assertFalse(valid_transaction(second, self.chain.chain, [first]))

    def test_snapshot_omission_and_duplicates_rejected(self):
        self.activate_bob()
        for stakes in ([], registered_stakes(self.chain.chain)[:1],
                       registered_stakes(self.chain.chain) * 2):
            block = candidate(self.chain.chain, self.alice)
            block.stakers = stakes
            sign_block(block, self.alice.private_key)
            self.assertFalse(self.chain.isValidBlock(block))

    def test_claimed_stake_must_match_registry(self):
        block = candidate(self.chain.chain, self.alice)
        block.staked_amt = 100000
        sign_block(block, self.alice.private_key)
        self.assertFalse(self.chain.isValidBlock(block))

    def test_signature_covers_consensus_fields(self):
        block = candidate(self.chain.chain, self.alice)
        original_hash = block.hash
        block.stakers[0].amt += 1
        self.assertNotEqual(block.hash, original_hash)
        self.assertFalse(self.chain.isValidBlock(block))

    def test_transaction_replay_in_previous_block(self):
        tx = transaction(self.chain, self.alice, self.bob.public_key_pem, 5)
        self.append(candidate(self.chain.chain, self.alice, [tx]))
        self.assertFalse(self.chain.isValidBlock(candidate(self.chain.chain, self.alice, [tx])))
        self.assertTrue(transaction_exists_in_block_list(self.chain.chain, tx, len(self.chain.chain)))

    def test_duplicate_transaction_inside_block(self):
        tx = transaction(self.chain, self.alice, self.bob.public_key_pem, 5)
        self.assertFalse(self.chain.isValidBlock(candidate(self.chain.chain, self.alice, [tx, tx])))

    def test_combined_overspending_is_rejected(self):
        txs = [transaction(self.chain, self.alice, self.bob.public_key_pem, 30) for _ in range(2)]
        self.assertFalse(self.chain.isValidBlock(candidate(self.chain.chain, self.alice, txs)))

    def test_fixed_precision_and_bad_amounts(self):
        self.assertEqual(amount_units(0.1) + amount_units(0.2), amount_units(0.3))
        for amount in (float("nan"), float("inf"), -1, 0, True, "1", 0.0000001):
            with self.assertRaises(ValueError):
                amount_units(amount)

    def test_transaction_cannot_cross_networks(self):
        tx = transaction(self.chain, self.alice, self.bob.public_key_pem, 1)
        tx.network = "another-network"
        tx.sign = self.alice.private_key.sign_deterministic(str(tx).encode())
        self.assertFalse(valid_transaction(tx, self.chain.chain))

    def test_live_and_history_validation_agree_after_five_blocks(self):
        for _ in range(5):
            tx = transaction(self.chain, self.alice, self.bob.public_key_pem, 1)
            block = candidate(self.chain.chain, self.alice, [tx])
            self.assertTrue(self.chain.isValidBlock(block))
            self.assertTrue(isvalidChain(self.chain.chain + [block]))
            self.chain.chain.append(block)
        balances, stakes = ledger_state(self.chain.chain)
        expected = balances[self.alice.public_key_pem] / COIN_UNITS - stakes[self.alice.public_key_pem].amt
        self.assertEqual(self.chain.calc_balance(self.alice.public_key_pem), expected)

    def test_wrong_parent_seed_slot_or_future_time_rejected(self):
        for field, value in (("prevHash", "bad"), ("seed", "bad"), ("slot", 0),
                             ("ts", int(time.time() * 1000) + 20000)):
            block = candidate(self.chain.chain, self.alice)
            setattr(block, field, value)
            sign_block(block, self.alice.private_key)
            self.assertFalse(self.chain.isValidBlock(block), field)

    def test_empty_stakes_do_not_crash(self):
        block = candidate(self.chain.chain, self.alice)
        block.stakers = []
        sign_block(block, self.alice.private_key)
        self.assertFalse(self.chain.isValidBlock(block))

    def test_forged_genesis_mint_rejected(self):
        genesis = copy.deepcopy(self.chain.chain[0])
        genesis.transactions[0].payload = 50000
        sign_block(genesis, self.alice.private_key)
        self.assertFalse(isvalidChain([genesis]))

    def test_foreign_genesis_cannot_replace_chain(self):
        saved = self.chain
        foreign, alice, _ = fresh_chain()
        foreign.chain.append(candidate(foreign.chain, alice))
        self.assertFalse(saved.rewrite(foreign.chain))

    def test_fork_tie_break_converges_independent_of_arrival_order(self):
        one, two, _ = self.proof()
        preferred = max([one, two], key=lambda b: b.hash)
        other = min([one, two], key=lambda b: b.hash)
        self.append(other)
        self.assertTrue(self.chain.rewrite([self.chain.chain[0], preferred]))
        self.assertFalse(self.chain.rewrite([self.chain.chain[0], other]))

    def test_heavier_chain_can_replace_a_longer_chain(self):
        first = candidate(self.chain.chain, self.alice)
        low_branch = self.chain.chain + [first]
        low_branch.append(candidate(low_branch, self.alice))
        low_branch.append(candidate(low_branch, self.alice))
        stake = transaction(self.chain, self.alice, "stake", 40)
        high_branch = self.chain.chain + [candidate(self.chain.chain, self.alice, [stake])]
        high_branch.append(candidate(high_branch, self.alice))
        self.assertGreater(weight_of_chain(high_branch), weight_of_chain(low_branch))
        self.chain.chain = low_branch
        self.assertTrue(self.chain.rewrite(high_branch))

    def test_identical_or_forged_evidence_cannot_slash(self):
        one, two, proof = self.proof()
        self.assertTrue(valid_evidence(proof, self.chain.chain))
        same = {"first": one.signed_header(), "second": one.signed_header()}
        self.assertFalse(valid_evidence(same, self.chain.chain))
        proof["second"]["sign"] = base64.b64encode(b"fake").decode()
        self.assertFalse(valid_evidence(proof, self.chain.chain))

    def test_different_creators_are_an_ordinary_fork(self):
        self.activate_bob()
        one = candidate(self.chain.chain, self.alice)
        two = candidate(self.chain.chain, self.bob)
        self.assertFalse(valid_evidence({"first": one.signed_header(), "second": two.signed_header()}, self.chain.chain))

    def test_slashing_is_confirmed_and_replayable_from_history(self):
        self.activate_bob()
        one, two, proof = self.proof()
        self.append(one)
        before, _ = ledger_state(self.chain.chain)
        penalty = candidate(self.chain.chain, self.bob, evidence=[proof])
        self.append(penalty)
        after, stakes = ledger_state(self.chain.chain)
        self.assertEqual(before[self.alice.public_key_pem] - after[self.alice.public_key_pem], 10 * COIN_UNITS)
        self.assertNotIn(self.alice.public_key_pem, stakes)
        self.assertTrue(isvalidChain(self.chain.chain))
        repeated = candidate(self.chain.chain, self.bob, evidence=[proof])
        self.assertFalse(self.chain.isValidBlock(repeated))

    def test_common_chain_bad_initialization(self):
        with self.assertRaises(ValueError):
            CommonChain()

    def test_storage_isolation_atomic_save_and_read_without_creation(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(storage_manager, "BASE_STORAGE_DIR", directory):
            self.assertIsNone(storage_manager.load_key("pos/absent"))
            self.assertFalse(Path(directory, "pos/absent").exists())
            storage_manager.save_key("alice", "pos/room/5000")
            storage_manager.save_key("bob", "pos/room/5001")
            self.assertEqual(storage_manager.load_key("pos/room/5000"), "alice")
            self.assertEqual(storage_manager.load_key("pos/room/5001"), "bob")
            self.assertFalse(list(Path(directory).rglob("*.tmp")))
            with self.assertRaises(ValueError):
                storage_manager.save_key("escape", "../escape")

    def test_contract_replay_and_forged_state(self):
        code = 'def increment(amount, state):\n    state["count"] = state.get("count", 0) + amount\n    return state, "ok"\n'
        fee = round((len(code) // 10 + 5) * 0.001, 6)
        deploy = transaction(self.chain, self.alice, "deploy", [code, fee])
        self.append(candidate(self.chain.chain, self.alice, [deploy]))
        from smart_contract.validation import contract_id
        from smart_contract.secure_executor import SecureContractExecutor
        cid = contract_id(deploy.sender, deploy.ts)
        result = SecureContractExecutor(code).run("increment", [2], {})
        self.assertTrue(result["success"], result)
        invoke = transaction(self.chain, self.alice, "invoke",
            [cid, "increment", [2], result["state"], round(result["gas_used"] * 0.001, 6)])
        self.assertTrue(validate_contract_transactions(self.chain.chain, [invoke]))
        invoke.payload[3] = {"count": 999}
        self.assertFalse(validate_contract_transactions(self.chain.chain, [invoke]))


class DummySocket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))


class PeerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.chain, alice, self.bob = fresh_chain()
        self.peer = Peer("127.0.0.1", 5000, "Alice", True, "n", "n")
        self.peer.wallet = alice
        self.peer.chain = self.chain
        self.peer.refresh_state()
        self.socket = DummySocket()

    async def asyncTearDown(self):
        Chain.instance = None

    def packet(self, **fields):
        return dict(protocol="pos-v2", room="legacy", network=self.peer.network_id,
                    id=str(uuid.uuid4()), **fields)

    async def test_received_valid_block_uses_same_rules(self):
        block = candidate(self.chain.chain, self.peer.wallet)
        await self.peer.handle_messages(self.socket, self.packet(type="new_block",
            block=block.to_dict(), sign=base64.b64encode(block.sign).decode()))
        self.assertEqual(len(self.chain.chain), 2)
        self.assertEqual(self.peer.rejected_messages, 0)

    async def test_received_transaction_binds_sender_key(self):
        tx = transaction(self.chain, self.peer.wallet, self.bob.public_key_pem, 1)
        await self.peer.handle_messages(self.socket, self.packet(type="new_tx",
            transaction=str(tx), sign=base64.b64encode(tx.sign).decode(), sender_pem=self.bob.public_key_pem))
        self.assertFalse(self.peer.mem_pool)
        self.assertEqual(self.peer.rejected_messages, 1)

    async def test_pending_duplicate_is_rejected(self):
        tx = transaction(self.chain, self.peer.wallet, self.bob.public_key_pem, 1)
        for _ in range(2):
            await self.peer.handle_messages(self.socket, self.packet(type="new_tx",
                transaction=str(tx), sign=base64.b64encode(tx.sign).decode(), sender_pem=tx.sender))
        self.assertEqual(len(self.peer.mem_pool), 1)

    async def test_malformed_message_does_not_kill_next_message(self):
        await self.peer.handle_messages(self.socket, [])
        await self.peer.handle_messages(self.socket, self.packet(type="new_block", block={}))
        await self.peer.handle_messages(self.socket, self.packet(type="ping"))
        self.assertEqual(self.socket.sent[-1]["type"], "pong")

    async def test_room_and_genesis_isolation(self):
        packet = self.packet(type="ping")
        packet["room"] = "other-room"
        await self.peer.handle_messages(self.socket, packet)
        packet = self.packet(type="ping")
        packet["network"] = "f" * 64
        await self.peer.handle_messages(self.socket, packet)
        self.assertEqual(self.peer.rejected_messages, 2)
        self.assertFalse(self.socket.sent)

    async def test_mempool_cleanup_does_not_skip_items(self):
        self.peer.mem_pool = [transaction(self.chain, self.peer.wallet, self.bob.public_key_pem, 1) for _ in range(3)]
        block = candidate(self.chain.chain, self.peer.wallet, self.peer.mem_pool)
        self.chain.chain.append(block)
        self.peer.refresh_state()
        self.assertEqual(self.peer.mem_pool, [])

    async def test_serialization_preserves_validity(self):
        block = candidate(self.chain.chain, self.peer.wallet)
        data = dict(block.to_dict(), sign=base64.b64encode(block.sign).decode())
        restored = self.peer.block_dict_to_block(data)
        self.assertEqual(restored.hash, block.hash)
        self.assertTrue(self.chain.isValidBlock(restored))

    async def test_orphaned_contract_call_cannot_stall_block_production(self):
        orphan = transaction(self.chain, self.peer.wallet, "invoke",
                             ["missing-after-reorg", "increment", [1], {"count": 1}, 0.001])
        self.assertTrue(valid_transaction(orphan, self.chain.chain))
        self.peer.mem_pool = [orphan]
        await self.peer.create_blocks(0)
        self.assertEqual(len(self.chain.chain), 2)
        self.assertEqual(self.peer.mem_pool, [])
        self.assertTrue(isvalidChain(self.chain.chain))

    async def test_old_protocol_is_rejected_clearly(self):
        with self.assertRaisesRegex(ValueError, "v2"):
            self.peer.block_dict_to_block({"transactions": []})

    async def test_web_commands_and_csrf_guard(self):
        app = create_app(self.peer, asyncio.get_running_loop(), 8500, "test-token")
        client = app.test_client()
        def request(method, path, **kwargs):
            return getattr(client, method)(path, base_url="http://127.0.0.1:8500", **kwargs)
        home = await asyncio.to_thread(request, "get", "/")
        self.assertEqual(home.status_code, 200)
        state = await asyncio.to_thread(request, "get", "/api/state")
        self.assertNotIn("private", state.get_data(as_text=True))
        denied = await asyncio.to_thread(request, "post", "/api/stakes", json={"amount": 12})
        self.assertEqual(denied.status_code, 403)
        headers = {"X-Simulation-Token": "test-token"}
        sent = await asyncio.to_thread(request, "post", "/api/transactions",
            json={"receiver": self.bob.public_key_pem, "amount": 2}, headers=headers)
        self.assertEqual(sent.status_code, 200, sent.get_data(as_text=True))
        invalid = await asyncio.to_thread(request, "post", "/api/transactions",
            json={"receiver": self.bob.public_key_pem, "amount": -2}, headers=headers)
        self.assertEqual(invalid.status_code, 400)
        cross_origin = await asyncio.to_thread(request, "post", "/api/stakes",
            json={"amount": 12}, headers=dict(headers, Origin="https://example.org"))
        self.assertEqual(cross_origin.status_code, 403)
        missing_contract = await asyncio.to_thread(request, "post", "/api/contracts/invoke",
            json={"contract_id": "missing", "function": "increment", "args": [1]}, headers=headers)
        self.assertEqual(missing_contract.status_code, 400)


if __name__ == "__main__":
    unittest.main()

