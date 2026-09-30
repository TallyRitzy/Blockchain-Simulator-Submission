"""JSON persistence; callers choose a consensus and node-specific namespace."""
import json
import os
import tempfile

BASE_STORAGE_DIR = os.path.dirname(os.path.abspath(__file__))


def get_consensus_dir(consensus, create=True):
    root = os.path.abspath(BASE_STORAGE_DIR)
    path = os.path.abspath(os.path.join(root, consensus))
    if os.path.commonpath([root, path]) != root:
        raise ValueError("Invalid storage namespace")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _save(name, value, consensus):
    directory = get_consensus_dir(consensus)
    temporary = None
    try:
        # Mission checkpoint: replace only after a complete durable JSON write.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, os.path.join(directory, name))
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _load(name, consensus):
    path = os.path.join(get_consensus_dir(consensus, create=False), name)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def save_node_id(node_id, consensus):
    _save("node_id.json", {"node_id": node_id}, consensus)


def load_node_id(consensus):
    data = _load("node_id.json", consensus)
    return data.get("node_id") if data else None


def save_key(private_key_pem, consensus):
    _save("keys.json", {"private_key_pem": private_key_pem}, consensus)


def load_key(consensus):
    data = _load("keys.json", consensus)
    return data.get("private_key_pem") if data else None


def save_chain(chain, consensus):
    _save("chain.json", chain, consensus)


def load_chain(consensus):
    return _load("chain.json", consensus)


def save_peers(peer_list, consensus):
    _save("peers.json", peer_list, consensus)


def load_peers(consensus):
    return _load("peers.json", consensus)