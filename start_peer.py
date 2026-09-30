import asyncio
import argparse
import sys
from consensus.poa.p2p import Peer as PoAPeer
from consensus.pos.p2p import Peer as PoSPeer
from consensus.pow.p2p import Peer as PoWPeer
from consensus.poa.mal_node import Peer as PoaMalPeer
from consensus.pos.mal_node import Peer as PosMalPeer
from consensus.pow.mal_node import Peer as PowMalPeer

def start_peer():
    host = input("Enter Host: ")
    port = int(input("Enter Port: "))
    name = input("Enter Name: ")
    consensus = input("Enter Consensus[poa/pos/pow] (default : pow): ")
    activate_disk_load = input("Do you like to load saved data if any(y/n): ")
    activate_disk_save = input("Do you like to continuously backup data to disk(y/n): ")
    action = input("Enter 'create' to create a network and 'connect' to connect to a network (default: create): ")
    bootstrap_host = None
    bootstrap_port = None
    if action == "connect":
        bootstrap_host = input("Enter host to connect: ")
        bootstrap_port = int(input("Enter port to connect: "))
    peer = None
    if consensus == "poa":
        mal=False
        mal_raw_input = input("Malcious? (y/n) ").strip().lower()
        if mal_raw_input == "y":
            mal = True
        elif mal_raw_input == "n":
            mal = False
        if(not mal):
            peer = PoAPeer(host, port, name, activate_disk_load, activate_disk_save)
        else:
            peer = PoaMalPeer(host, port, name, activate_disk_load, activate_disk_save)
        peer.name_to_node_id_dict[peer.name.lower()] = peer.node_id
        peer.node_id_to_name_dict[peer.node_id] = peer.name.lower()

    elif consensus == "pos":
        mal=False
        mal_raw_input = input("Malcious? (y/n) ").strip().lower()
        if mal_raw_input == "y":
            mal = True
        elif mal_raw_input == "n":
            mal = False
        
        if(not mal):
            staker = True
            staker_raw_input = input("Staker? (y/n) ").strip().lower()
            if staker_raw_input == "y":
                staker = True
            elif staker_raw_input == "n":
                staker = False
            peer = PoSPeer(host, port, name, staker, activate_disk_load, activate_disk_save)
        else:
            peer = PosMalPeer(host, port, name, True, activate_disk_load, activate_disk_save)

    else:        
        mal=False
        mal_raw_input = input("Malcious? (y/n) ").strip().lower()
        if mal_raw_input == "y":
            mal = True
        elif mal_raw_input == "n":
            mal = False
        
        if(not mal):
            miner = True
            miner_raw_input = input("Miner? (y/n) ").strip().lower()
            if miner_raw_input == "y":
                miner = True
            elif miner_raw_input == "n":
                miner = False
            peer = PoWPeer(host, port, name, miner, activate_disk_load, activate_disk_save)
        else:
            peer = PowMalPeer(host, port, name, True, activate_disk_load, activate_disk_save)


    try:
        asyncio.run(peer.start(bootstrap_host, bootstrap_port))
    except KeyboardInterrupt:
        print("\nShutting Down...")

def start_configured_peer():
    parser = argparse.ArgumentParser(description="Run a PoS node with room discovery and a local web interface")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--room", help="Room ID shared by the participating nodes")
    parser.add_argument("--signalling", default="ws://127.0.0.1:8765")
    parser.add_argument("--create-room", action="store_true")
    parser.add_argument("--web-port", type=int)
    parser.add_argument("--no-web", action="store_true")
    parser.add_argument("--cli", action="store_true", help="Also show the original terminal menu")
    parser.add_argument("--observer", action="store_true", help="Observe and transact without producing blocks")
    parser.add_argument("--malicious", action="store_true", help="Deliberately double-sign winning blocks")
    parser.add_argument("--save", action="store_true", help="Save this room/node's wallet and chain")
    parser.add_argument("--data-dir", help="Optional directory for node storage")
    parser.add_argument("--epoch-seconds", type=int, default=60, help="Slot duration for a NEW network (default 60)")
    parser.add_argument("--load", action="store_true", help="Load this room/node's saved wallet and chain")
    parser.add_argument("--bootstrap-host")
    parser.add_argument("--bootstrap-port", type=int)
    args = parser.parse_args()
    if not 2 <= args.epoch_seconds <= 3600:
        parser.error("--epoch-seconds must be between 2 and 3600")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.create_room and not args.room:
        parser.error("--create-room requires --room")
    if args.create_room and args.observer:
        parser.error("The room creator must produce blocks initially")
    if args.room and args.bootstrap_host:
        parser.error("Choose room discovery or a bootstrap address")
    if bool(args.bootstrap_host) != bool(args.bootstrap_port):
        parser.error("Provide both bootstrap host and port")
    web_port = None if args.no_web else (args.web_port if args.web_port is not None else args.port + 3000)
    if web_port is not None and (not 1 <= web_port <= 65535 or web_port == args.port):
        parser.error("Use a valid --web-port different from the peer port")
    if args.data_dir:
        from storage import storage_manager
        storage_manager.BASE_STORAGE_DIR = args.data_dir
    peer_type = PosMalPeer if args.malicious else PoSPeer
    try:
        peer = peer_type(args.host, args.port, args.name, not args.observer,
                         "y" if args.load else "n", "y" if args.save else "n",
                         room_id=args.room or "legacy", epoch_seconds=args.epoch_seconds)
        asyncio.run(peer.start(args.bootstrap_host, args.bootstrap_port,
            signalling_url=args.signalling if args.room else None,
            create_room=args.create_room, web_port=web_port, no_cli=not args.cli))
    except KeyboardInterrupt:
        print("\nNode stopped.")
    except (ValueError, OSError) as exc:
        print(f"Unable to start node: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__=="__main__":
    if len(sys.argv) > 1:
        start_configured_peer()
    else:
        start_peer()