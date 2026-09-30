"""Proof-of-stake data and shared rules for miners, receivers and history."""
import base64
import hashlib
import json
import time
import uuid
from decimal import Decimal, InvalidOperation
from typing import List
from ecdsa import VerifyingKey
from shared_blockchain_structures import (
    Transaction, BaseBlock, CommonChain, Wallet,
    txs_to_json_digestable_form, transaction_exists_in_block_list,
)

GAS_PRICE = 0.001
MAX_OUTPUT = 2**256
EPOCH_TIME = 60
COIN_UNITS = 1_000_000
INITIAL_STAKE = 10
BLOCK_REWARD = 6
PROTOCOL_VERSION = 2


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def amount_units(value, allow_zero=False):
    if type(value) not in (int, float):
        raise ValueError("Amount must be a number")
    try:
        number = Decimal(str(value))
        units = number * COIN_UNITS
        if (not number.is_finite() or number < 0 or
                (not allow_zero and number == 0) or number > 10**12 or
                units != units.to_integral_value()):
            raise ValueError("Use a positive amount with at most six decimal places")
        return int(units)
    except InvalidOperation as exc:
        raise ValueError("Invalid amount") from exc


def valid_public_key(key):
    try:
        return isinstance(key, str) and VerifyingKey.from_pem(key).to_pem().decode() == key
    except Exception:
        return False


def transaction_amount(tx):
    return tx.payload[-1] if tx.receiver in ("deploy", "invoke") else tx.payload


class Stake:
    def __init__(self, staker: str, amt: int, ts=None, id=None):
        self.id = id or str(uuid.uuid4())
        self.staker = staker
        self.amt = amt
        self.ts = time.time() if ts is None else ts
        self.sign = None

    def to_dict(self):
        return {"id": self.id, "staker": self.staker, "amt": self.amt, "ts": self.ts}

    def __str__(self):
        return canonical(self.to_dict())


class Block(BaseBlock):
    def __init__(self, prevHash: str, transactions: List[Transaction], ts=None, id=None):
        super().__init__(prevHash, list(transactions), ts, id)
        self.version = PROTOCOL_VERSION
        self.epoch_seconds = EPOCH_TIME
        self.creator = ""
        self.staked_amt = 0
        self.stakers = []
        self.slot = 0
        self.seed = ""
        self.vrf_proof = None
        self.sign = None
        self.slash_evidence = []
        self.is_valid = True
        self.slash_creator = False

    def to_dict(self):
        return {
            "version": self.version, "epoch_seconds": self.epoch_seconds, "id": self.id, "prevHash": self.prevHash,
            "transactions": txs_to_json_digestable_form(self.transactions),
            "ts": self.ts, "creator": self.creator, "staked_amt": self.staked_amt,
            "files": self.files, "slot": self.slot, "seed": self.seed,
            "stakers": [s.to_dict() for s in self.stakers],
            "vrf_proof_b64": base64.b64encode(self.vrf_proof).decode() if self.vrf_proof else "",
            "slash_evidence": self.slash_evidence,
        }

    def to_dict_with_stakers(self):
        return self.to_dict()

    def __str__(self):
        return canonical(self.to_dict())

    @property
    def hash(self):
        return hashlib.sha256(str(self).encode()).hexdigest()

    def signing_header(self):
        return {"hash": self.hash, "creator": self.creator, "prevHash": self.prevHash,
                "slot": self.slot, "seed": self.seed, "staked_amt": self.staked_amt}

    def signed_header(self):
        return {"header": self.signing_header(), "sign": base64.b64encode(self.sign).decode()}

    def is_equal(self, other):
        return isinstance(other, Block) and self.hash == other.hash


def sign_block(block, private_key):
    block.sign = private_key.sign_deterministic(
        canonical(block.signing_header()).encode(), hashfunc=hashlib.sha256)


def verify_header(record):
    header = record["header"]
    if set(header) != {"hash", "creator", "prevHash", "slot", "seed", "staked_amt"}:
        return False
    if not valid_public_key(header["creator"]):
        return False
    if not isinstance(header["hash"], str) or len(header["hash"]) != 64:
        return False
    return VerifyingKey.from_pem(header["creator"]).verify(
        base64.b64decode(record["sign"], validate=True), canonical(header).encode(),
        hashfunc=hashlib.sha256)


def epoch_seed(block_list, slot):
    return hashlib.sha256(canonical({"network": block_list[0].hash,
        "parent": block_list[-1].hash, "slot": slot}).encode()).hexdigest()


def slot_at(block_list, timestamp=None):
    milliseconds = int(time.time() * 1000) if timestamp is None else timestamp
    return max(0, (milliseconds - block_list[0].ts) // (block_list[0].epoch_seconds * 1000))


def lottery_output(public_key, seed):
    # Mission lottery tickets depend on committed identity, never retryable signatures.
    return int(hashlib.sha256(canonical([seed, public_key]).encode()).hexdigest(), 16)


def lottery_wins(public_key, seed, stake, total):
    return (type(stake) is int and type(total) is int and 0 < stake <= total and
            lottery_output(public_key, seed) * total < stake * MAX_OUTPUT)


def evidence_id(evidence):
    h = evidence["first"]["header"]
    return (h["creator"], h["prevHash"], h["slot"])


def used_evidence(block_list):
    return {evidence_id(e) for b in block_list for e in b.slash_evidence}


def _apply_transaction(balances, stakes, tx):
    if tx.receiver == "stake":
        if tx.payload:
            stakes[tx.sender] = Stake(tx.sender, tx.payload, tx.ts, tx.id)
        else:
            stakes.pop(tx.sender, None)
        return
    units = amount_units(transaction_amount(tx))
    balances[tx.sender] = balances.get(tx.sender, 0) - units
    if tx.receiver not in ("deploy", "invoke"):
        balances[tx.receiver] = balances.get(tx.receiver, 0) + units


def _apply_penalties(balances, stakes, evidence):
    for proof in evidence:
        header = proof["first"]["header"]
        key = header["creator"]
        current = stakes.pop(key, None)
        if current:
            penalty = min(current.amt, header["staked_amt"]) * COIN_UNITS
            balances[key] = balances.get(key, 0) - penalty


def ledger_state(block_list, pending=None, evidence=None):
    balances, stakes = {}, {}
    for index, block in enumerate(block_list):
        if index == 0:
            balances[block.creator] = (50 + BLOCK_REWARD) * COIN_UNITS
            stakes[block.creator] = Stake(block.creator, INITIAL_STAKE, block.ts / 1000, "genesis")
            continue
        _apply_penalties(balances, stakes, block.slash_evidence)
        for tx in block.transactions:
            _apply_transaction(balances, stakes, tx)
        balances[block.creator] = balances.get(block.creator, 0) + BLOCK_REWARD * COIN_UNITS
    _apply_penalties(balances, stakes, evidence or [])
    for tx in pending or []:
        _apply_transaction(balances, stakes, tx)
    return balances, stakes


def registered_stakes(block_list):
    _, stakes = ledger_state(block_list)
    return [stakes[key] for key in sorted(stakes)]


def calc_balance_block_list(block_list, publicKey, i, mem_pool=None, currStakes=None):
    balances, stakes = ledger_state(block_list[:i], mem_pool)
    locked = stakes[publicKey].amt * COIN_UNITS if publicKey in stakes else 0
    return (balances.get(publicKey, 0) - locked) / COIN_UNITS


def weight_of_chain(block_list):
    return sum(sum(s.amt for s in block.stakers) for block in block_list[1:])


def chain_rank(block_list):
    return (weight_of_chain(block_list), block_list[-1].hash)


def valid_transaction(tx, block_list, pending=None, evidence=None):
    try:
        pending = pending or []
        if (not isinstance(tx.id, str) or not tx.id or len(tx.id) > 128 or
                not block_list or tx.network != block_list[0].hash or
                type(tx.ts) not in (int, float) or not 0 < tx.ts < time.time() + 10 or
                not valid_public_key(tx.sender) or
                any(t.id == tx.id for b in block_list for t in b.transactions) or
                any(t.id == tx.id for t in pending) or not tx.is_valid_signature()):
            return False
        balances, stakes = ledger_state(block_list, pending, evidence)
        if tx.receiver == "stake":
            if type(tx.payload) is not int or tx.payload <= 0:
                return False
            current = stakes.get(tx.sender)
            if current and tx.payload < current.amt:
                return False
            if tx.sender not in stakes and len(stakes) >= 256:
                return False
            if any(t.sender == tx.sender and t.receiver == "stake" for t in pending):
                return False
            return tx.payload * COIN_UNITS <= balances.get(tx.sender, 0)
        if tx.receiver == "deploy":
            if (not isinstance(tx.payload, list) or len(tx.payload) != 2 or
                    not isinstance(tx.payload[0], str) or not 0 < len(tx.payload[0]) <= 16000):
                return False
            if amount_units(tx.payload[-1]) != (len(tx.payload[0]) // 10 + 5) * 1000:
                return False
        elif tx.receiver == "invoke":
            if (not isinstance(tx.payload, list) or len(tx.payload) != 5 or
                    not all(isinstance(x, str) for x in tx.payload[:2]) or
                    not isinstance(tx.payload[2], list) or not isinstance(tx.payload[3], dict)):
                return False
        elif not valid_public_key(tx.receiver):
            return False
        units = amount_units(transaction_amount(tx))
        locked = stakes[tx.sender].amt * COIN_UNITS if tx.sender in stakes else 0
        return units <= balances.get(tx.sender, 0) - locked
    except (ValueError, TypeError, KeyError, IndexError, OverflowError):
        return False


def valid_evidence(proof, block_list):
    try:
        a, b = proof["first"]["header"], proof["second"]["header"]
        if not verify_header(proof["first"]) or not verify_header(proof["second"]):
            return False
        if a["hash"] == b["hash"] or any(a[k] != b[k] for k in
                ("creator", "prevHash", "slot", "seed", "staked_amt")):
            return False
        parent = next(i for i, block in enumerate(block_list) if block.hash == a["prevHash"])
        prefix = block_list[:parent + 1]
        registry = {s.staker: s.amt for s in registered_stakes(prefix)}
        return (type(a["slot"]) is int and a["slot"] > prefix[-1].slot and
                a["seed"] == epoch_seed(prefix, a["slot"]) and
                registry.get(a["creator"]) == a["staked_amt"] and
                lottery_wins(a["creator"], a["seed"], a["staked_amt"], sum(registry.values())))
    except Exception:
        return False


def valid_block(block, prefix, now=None):
    try:
        if (not isinstance(block, Block) or block.version != PROTOCOL_VERSION or
                type(block.epoch_seconds) is not int or not 2 <= block.epoch_seconds <= 3600 or
                type(block.ts) is not int or type(block.slot) is not int or
                not 0 < block.ts <= (time.time() if now is None else now) * 1000 + 10000 or
                not isinstance(block.id, str) or not block.id or len(block.id) > 128 or
                not isinstance(block.files, dict) or len(block.files) > 100 or
                len(block.transactions) > 1000 or len(block.slash_evidence) > 16 or
                not valid_public_key(block.creator) or not verify_header(block.signed_header())):
            return False
        if any(not isinstance(k, str) or not isinstance(v, str) or len(k) > 256 or len(v) > 500
               for k, v in block.files.items()):
            return False
        if not prefix:
            if (block.prevHash is not None or block.slot != 0 or block.stakers or
                    block.staked_amt != 0 or block.seed or block.vrf_proof or block.files or
                    block.slash_evidence or len(block.transactions) != 1):
                return False
            tx = block.transactions[0]
            return (tx.sender == "Genesis" and tx.receiver == block.creator and
                    type(tx.payload) is int and tx.payload == 50 and
                    isinstance(tx.id, str) and bool(tx.id) and
                    type(tx.ts) in (int, float) and 0 < tx.ts <= block.ts / 1000 + 1)
        if (block.prevHash != prefix[-1].hash or block.epoch_seconds != prefix[0].epoch_seconds or block.ts <= prefix[-1].ts or
                block.slot <= prefix[-1].slot or block.slot != slot_at(prefix, block.ts) or
                block.seed != epoch_seed(prefix, block.slot)):
            return False
        expected = registered_stakes(prefix)
        if [s.to_dict() for s in block.stakers] != [s.to_dict() for s in expected]:
            return False
        registry = {s.staker: s.amt for s in expected}
        if registry.get(block.creator) != block.staked_amt or not lottery_wins(
                block.creator, block.seed, block.staked_amt, sum(registry.values())):
            return False
        VerifyingKey.from_pem(block.creator).verify(block.vrf_proof, block.seed.encode(), hashfunc=hashlib.sha256)
        seen = used_evidence(prefix)
        for proof in block.slash_evidence:
            key = evidence_id(proof)
            if key in seen or not valid_evidence(proof, prefix):
                return False
            seen.add(key)
        pending = []
        for tx in block.transactions:
            if not valid_transaction(tx, prefix, pending, block.slash_evidence):
                return False
            pending.append(tx)
        if any(t.receiver in ("deploy", "invoke") for t in pending):
            from smart_contract.validation import validate_contract_transactions
            if not validate_contract_transactions(prefix, pending):
                return False
        return True
    except Exception:
        return False


def isvalidChain(blockList):
    if not isinstance(blockList, list) or not blockList:
        return False
    return all(valid_block(block, blockList[:i]) for i, block in enumerate(blockList))


class Chain(CommonChain):
    instance = None

    def __init__(self, publicKey=None, privatekey=None, blockList=None, epoch_seconds=EPOCH_TIME):
        if Chain.instance is not None:
            raise ValueError("Run each node in its own process")
        if publicKey and privatekey and blockList is None:
            genesis = Block(None, [Transaction(50, "Genesis", publicKey)])
            if type(epoch_seconds) is not int or not 2 <= epoch_seconds <= 3600:
                raise ValueError("Slot duration must be between 2 and 3600 seconds")
            genesis.epoch_seconds = epoch_seconds
            genesis.creator = publicKey
            sign_block(genesis, privatekey)
            super().__init__(genesis_block=genesis)
        elif blockList and publicKey is None and isvalidChain(blockList):
            super().__init__(block_list=blockList)
        else:
            raise ValueError("Invalid or incompatible PoS chain; start a fresh v2 network")
        Chain.instance = self

    def to_block_dict_list(self):
        result = []
        for block in self.chain:
            data = block.to_dict_with_stakers()
            data["sign"] = base64.b64encode(block.sign).decode()
            result.append(data)
        return result

    def rewrite(self, blockList):
        candidate = self.validated_candidate(blockList)
        if candidate is None or chain_rank(candidate) <= chain_rank(self.chain):
            return False
        self.chain = candidate
        return True

    def validated_candidate(self, blockList):
        try:
            if not blockList or blockList[0].hash != self.chain[0].hash:
                return None
            shared = 0
            while shared < min(len(self.chain), len(blockList)) and self.chain[shared].hash == blockList[shared].hash:
                shared += 1
            # Flight records already verified locally can anchor the new trajectory.
            candidate = self.chain[:shared] + list(blockList[shared:])
            for index in range(shared, len(candidate)):
                if not valid_block(candidate[index], candidate[:index]):
                    return None
            return candidate
        except (ValueError, TypeError, AttributeError):
            return None

    def isValidBlock(self, block):
        return valid_block(block, self.chain)

    def calc_balance(self, publicKey, pending_transactions=None, current_stakes=None):
        return calc_balance_block_list(self.chain, publicKey, len(self.chain), pending_transactions)

    def epoch_seed(self, slot=None):
        return epoch_seed(self.chain, slot_at(self.chain) if slot is None else slot)

    def checkEquivalence(self, block_list):
        for i in range(min(len(self.chain), len(block_list))):
            if not self.chain[i].is_equal(block_list[i]):
                return i
        return -1

