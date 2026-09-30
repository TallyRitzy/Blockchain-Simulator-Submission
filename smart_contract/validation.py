"""Replay contract calls against the same history on every PoS node."""
import copy
import hashlib
import json
from smart_contract.secure_executor import SecureContractExecutor


def contract_id(sender, timestamp):
    return hashlib.sha256(f"{sender}:{timestamp}".encode()).hexdigest()


def contract_context(blocks, pending=()):
    codes, states = {}, {}
    for tx in [t for b in blocks for t in b.transactions] + list(pending):
        if tx.receiver == "deploy":
            codes[contract_id(tx.sender, tx.ts)] = tx.payload[0]
        elif tx.receiver == "invoke":
            states[tx.payload[0]] = copy.deepcopy(tx.payload[3])
    return codes, states


def validate_contract_transactions(blocks, transactions):
    codes, states = contract_context(blocks)
    for tx in transactions:
        if tx.receiver == "deploy":
            codes[contract_id(tx.sender, tx.ts)] = tx.payload[0]
        elif tx.receiver == "invoke":
            cid, name, args, expected_state, fee = tx.payload
            if cid not in codes:
                return False
            result = SecureContractExecutor(codes[cid]).run(name, args, copy.deepcopy(states.get(cid, {})))
            if not result["success"] or result["state"] != expected_state:
                return False
            if round(result["gas_used"] * 0.001, 6) != fee:
                return False
            json.dumps(result["state"], allow_nan=False)
            states[cid] = result["state"]
    return True
