import asyncio, websockets, traceback, hashlib
import argparse, json, uuid, base64
import threading, socket, os, subprocess
from datetime import datetime, timedelta
from typing import Set, Dict, List, Tuple, Any
from consensus.pos.blockchain_structures import (
    Transaction, Stake, Block, Wallet, Chain, isvalidChain, weight_of_chain,
    valid_transaction, valid_block, valid_public_key, valid_evidence, evidence_id,
    registered_stakes, used_evidence, sign_block, slot_at, lottery_wins,
    ledger_state, canonical, chain_rank, COIN_UNITS, PROTOCOL_VERSION,
)
from network_utils import RecentMessageIds
from collections import deque
import time
from ipfs.ipfs import addToIpfs, download_ipfs_file_subprocess
from smart_contract.contracts_db import SmartContractDatabase
from smart_contract.secure_executor import SecureContractExecutor
from storage.storage_manager import save_key, load_key, save_chain, load_chain, save_peers, load_peers
from ecdsa import VerifyingKey, BadSignatureError
import tempfile
from pathlib import Path
import ast

MAX_CONNECTIONS = 8
MAX_OUTPUT=2**256
EPOCH_TIME=60
GAS_PRICE = 0.001 # coin per gas unit
BASE_DEPLOY_COST = 5
CONSENSUS ="pos"

class VrfThresholdException(Exception):
    pass

def get_random_element(s):
    """
        Return a random element from a set
    """
    import random
    return random.choice(list(s)) if s else None

def normalize_endpoint(ep):
    """
        Return host resolved into ipv4 address and port converted into int datatype - maintains consistency in the code
    """
    host, port = ep
    return (socket.gethostbyname(host), int(port))

def get_contract_code_from_notepad():
    # Create a temporary file with a .py extension
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode='w+', encoding='utf-8') as tmp_file:
        temp_filename = tmp_file.name
        tmp_file.write("# Write your smart contract function here.\n")
        tmp_file.write("def contract_logic(parameter1, parameter2, parameter3, state):\n")
        tmp_file.write("    # your code here\n")
        tmp_file.write("    return state, 'some message'\n")
    
    # Open it in Notepad (waits until closed)
    subprocess.call(["notepad.exe", temp_filename])

    # Read the edited code
    with open(temp_filename, 'r', encoding='utf-8') as f:
        contract_code = f.read()

    # Optional: remove the temp file
    os.remove(temp_filename)

    return contract_code

class Peer:
    def __init__(self, host, port, name, staker, activate_disk_load, activate_disk_save, room_id="legacy", epoch_seconds=EPOCH_TIME):
        self.host, self.port = normalize_endpoint((host, port))
        self.name, self.staker = name.strip(), staker
        if not self.name or len(self.name) > 40 or not 1 <= self.port <= 65535:
            raise ValueError("Provide a name (1-40 characters) and a valid port")
        self.room_id = room_id
        self.epoch_seconds = epoch_seconds
        room_key = hashlib.sha256(room_id.encode()).hexdigest()[:16]
        self.storage_namespace = f"pos/v2/{room_key}/{port}"
        self.activate_disk_save = activate_disk_save
        self.name_to_public_key_dict = {}
        self.known_peers = {}
        self.wallet = None
        self.chain = None
        self.network_id = ""
        self.server_connections, self.client_connections = set(), set()
        self.outbound_peers, self.connecting_peers = set(), set()
        self.connected_identities = {}
        self.seen_message_ids = RecentMessageIds()
        self.got_pong, self.have_sent_peer_info = {}, {}
        self.mem_pool = []
        self.mem_pool_lock = asyncio.Lock()
        self.state_lock = asyncio.Lock()
        self.file_hashes = {}
        self.file_hashes_lock = asyncio.Lock()
        self.current_stakes, self.current_stakers = set(), {}
        self.curr_stakers_condition = asyncio.Condition()
        self.create_block_condition = asyncio.Condition()
        self.staked_amt = 0
        self.last_epoch_end_ts = datetime.now()
        self.last_attempt_slot = -1
        self.mine_task = None
        self.contractsDB = SmartContractDatabase()
        self.pending_evidence = {}
        self.events = deque(maxlen=100)
        self.rejected_messages = 0
        self.ready = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.signalling_connected = False
        self.web_server = None
        self.daemon_process = None
        self.ipfs_port, self.gateway_port = port + 50, port + 81
        self.swarm_tcp, self.swarm_udp = port + 2, port + 3
        self.repo_path = Path(__file__).resolve().parents[2] / "storage" / self.storage_namespace / "ipfs"
        self.env = os.environ.copy()
        self.env["IPFS_PATH"] = str(self.repo_path)
        if activate_disk_load == "y":
            self.load_key_from_disk()
            self.load_known_peers_from_disk()
            self.load_chain_from_disk()
        if self.wallet is None:
            self.wallet = Wallet()
            if activate_disk_save == "y":
                self.save_key_to_disk()
        if self.chain:
            self.network_id = self.chain.chain[0].hash
            self.refresh_state()

    def save_key_to_disk(self):
        key = self.wallet.private_key_pem
        save_key(key, self.storage_namespace)

    def load_key_from_disk(self):
        key = load_key(self.storage_namespace)
        if not key:
            self.wallet = None
            return
        self.wallet = Wallet(key)

    def load_chain_from_disk(self):
        data = load_chain(self.storage_namespace)
        if data:
            blocks = [self.block_dict_to_block(item) for item in data]
            self.chain = Chain(blockList=blocks)

    def save_chain_to_disk(self):
        chain = Chain.instance.to_block_dict_list()
        save_chain(chain, self.storage_namespace)

    def save_known_peers_to_disk(self):
        content = {}
        for key, value in self.known_peers.items():
            content[json.dumps(key)] = list(value)
        save_peers(content, self.storage_namespace)

    def load_known_peers_from_disk(self):
        content = load_peers(self.storage_namespace) or {}
        for key, value in content.items():
            endpoint = normalize_endpoint(tuple(json.loads(key)))
            if len(value) == 2 and valid_public_key(value[1]):
                self.known_peers[endpoint] = tuple(value)
                self.name_to_public_key_dict[value[0].lower()] = value[1]

    async def send_peer_info(self, websocket):
        data = {"host": self.host, "port": self.port, "name": self.name,
                "public_key": self.wallet.public_key_pem}
        signature = self.wallet.private_key.sign_deterministic(
            canonical([self.room_id, self.network_id, data]).encode(), hashfunc=hashlib.sha256)
        packet = {"type": "peer_info", "id": str(uuid.uuid4()), "data": data,
                  "identity_sign": base64.b64encode(signature).decode()}
        await websocket.send(self.encode_packet(packet))

    async def send_known_peers(self, websocket):
        peers = [{"host": h, "port": p, "name": n, "public_key": key}
                 for (h, p), (n, key) in self.known_peers.items()]
        peers.append({"host": self.host, "port": self.port, "name": self.name,
                      "public_key": self.wallet.public_key_pem})
        await websocket.send(self.encode_packet({"type": "known_peers",
            "id": str(uuid.uuid4()), "peers": peers}))

    def block_dict_to_block(self, data):
        if not isinstance(data, dict) or data.get("version") != PROTOCOL_VERSION:
            raise ValueError("Expected a PoS v2 block; old saved chains are incompatible")
        if not isinstance(data.get("transactions"), list) or len(data["transactions"]) > 1000:
            raise ValueError("Invalid transaction list")
        txs = []
        for item in data["transactions"]:
            tx = Transaction(item["payload"], item["sender"], item["receiver"], item["id"], item["ts"], network=item.get("network"))
            if tx.sender != "Genesis":
                tx.sign = base64.b64decode(item["sign"], validate=True)
            txs.append(tx)
        block = Block(data["prevHash"], txs, data["ts"], data["id"])
        for key in ("creator", "staked_amt", "files", "slot", "seed", "slash_evidence", "epoch_seconds"):
            setattr(block, key, data[key])
        if not isinstance(data["stakers"], list) or len(data["stakers"]) > 256:
            raise ValueError("Invalid stake snapshot")
        block.stakers = [self.stake_dict_to_stake(s) for s in data["stakers"]]
        block.vrf_proof = base64.b64decode(data["vrf_proof_b64"], validate=True) or None
        if data.get("sign"):
            block.sign = base64.b64decode(data["sign"], validate=True)
        return block
    
    def stake_dict_to_stake(self, data):
        if not isinstance(data, dict) or set(data) != {"id", "staker", "amt", "ts"}:
            raise ValueError("Invalid stake record")
        if type(data["amt"]) is not int or data["amt"] <= 0 or not valid_public_key(data["staker"]):
            raise ValueError("Invalid registered stake")
        return Stake(data["staker"], data["amt"], data["ts"], data["id"])

    def valid_deploy_transaction(self, payload):
        contract_code = payload[0]
        gas_used = len(contract_code)//10 + BASE_DEPLOY_COST
        amount = gas_used * GAS_PRICE
        if amount != payload[-1]:
            return False
        return True

    def valid_invoke_transaction(self, payload):
        contract_id = payload[0]
        func_name = payload[1]
        args = payload[2]
        response = self.run_contract([contract_id, func_name, args])
        if(response["error"] != None):
            return False
        state = response["state"]
        gas_used = response["gas_used"]
        amount = gas_used * GAS_PRICE
        if state != payload[3]:
            return False
        if amount != payload[-1]:
            return False
        return True

    def get_unique_name(self, base_name):
        existing_names = []
        for key, value in self.known_peers.items():
            existing_names.append(value[0].lower())

        existing_names.append(self.name)
        
        base_name = base_name.lower()
        if base_name not in existing_names:
            return base_name
        
        counter = 1
        while True:
            new_name = f"{base_name}{counter}"
            if new_name not in existing_names:
                return new_name
            counter += 1

    async def handle_messages(self, websocket, msg):
        try:
            if not isinstance(msg, dict):
                raise ValueError("Message must be an object")
            if msg.get("protocol") != "pos-v2" or msg.get("room") != self.room_id:
                raise ValueError("Different protocol or room")
            network = msg.get("network")
            if not self.network_id and msg.get("type") == "pong":
                if not isinstance(network, str) or len(network) != 64:
                    raise ValueError("Missing network identity")
                self.network_id = network
            if self.network_id and network != self.network_id:
                if not (msg.get("type") == "ping" and network == ""):
                    raise ValueError("Different genesis block")
            identity = msg.get("id")
            if not isinstance(identity, str) or not 0 < len(identity) <= 128:
                raise ValueError("Invalid message ID")
            if identity in self.seen_message_ids:
                return
            self.seen_message_ids.add(identity)
            if msg.get("type") in {"new_tx", "new_block", "chain", "slash_announcement"}:
                async with self.state_lock:
                    await self.process_message(websocket, msg)
            else:
                await self.process_message(websocket, msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.rejected_messages += 1
            self.record_event("rejected", str(exc)[:180])

    async def verify_and_slash(self, block1, block2, pos, block_list):
        proof = {"first": block1.signed_header(), "second": block2.signed_header()}
        if not valid_evidence(proof, self.chain.chain):
            return False
        key = evidence_id(proof)
        if key in self.pending_evidence or key in used_evidence(self.chain.chain):
            return False
        self.pending_evidence[key] = proof
        self.record_event("evidence", "Double signing detected; signed evidence queued")
        await self.broadcast_message({"type": "slash_announcement", "id": str(uuid.uuid4()), "evidence": proof})
        return True
        # Now the receiver should make sure that the block1 creator signed both the blocks and it is he that is penalized in slash_block, also check my signature 
        # Then if Chain.instance.chain[pos]==block1 or block2 then make that block invalid and slash the creator
        
    async def handle_connections(self, websocket):
        if len(self.server_connections) >= 32:
            await websocket.close(code=1013, reason="Peer capacity reached")
            return
        self.server_connections.add(websocket)
        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw, parse_constant=self.reject_constant)
                except (ValueError, TypeError):
                    self.rejected_messages += 1
                    continue
                await self.handle_messages(websocket, msg)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.server_connections.discard(websocket)
            self.connected_identities.pop(websocket, None)

    async def broadcast_message(self, pkt):
        encoded = self.encode_packet(pkt)
        targets = list(self.server_connections | self.client_connections)
        async def send(ws):
            try:
                await asyncio.wait_for(ws.send(encoded), timeout=2)
            except Exception:
                await ws.close()
        await asyncio.gather(*(send(ws) for ws in targets))

    async def create_and_broadcast_tx(self, receiver_public_key, payload):
        self.require_ready()
        async with self.state_lock:
            tx = Transaction(payload, self.wallet.public_key_pem, receiver_public_key, network=self.network_id)
            tx.sign = self.wallet.private_key.sign_deterministic(str(tx).encode())
            if len(self.mem_pool) >= 1000 or not valid_transaction(tx, self.chain.chain, self.mem_pool):
                raise ValueError("Invalid amount, duplicate stake update, or insufficient available balance")
            if not await self.validate_contract_batch(self.mem_pool + [tx]):
                raise ValueError("Invalid contract result or fee")
            self.mem_pool.append(tx)
            packet = {"type": "new_tx", "id": str(uuid.uuid4()), "transaction": str(tx),
                      "sign": base64.b64encode(tx.sign).decode(), "sender_pem": tx.sender}
            self.seen_message_ids.add(packet["id"])
            self.record_event("transaction", "Submitted " + tx.id[:12])
        await self.broadcast_message(packet)
        return tx.id

    def get_contract_state(self, contract_id):
        for block in reversed(Chain.instance.chain):
            for transaction in reversed(block.transactions):
                if transaction.receiver == "invoke" and transaction.payload[0] == contract_id:
                    return transaction.payload[3]
        return {}

    async def user_input_handler(self):
        while not self.stop_event.is_set():
            try:
                await self._user_input_loop()
                return
            except (ValueError, OSError) as exc:
                print(f"Command could not be completed: {exc}")

    async def _user_input_loop(self):
        """
            A function to constantly take input from the user 
            about whom to send and how much
        """
        while True:
            print("Block Chain Menu\n***************")
            if(self.staker):
                print("0) Quit\n1) Add Transaction\n2) View balance\n3) Print Chain\n4) Print Pending Transactions\n5) Print Current Stakers\n6) Time since last epoch\n7) Send Files\n8) Download Files\n9) Stake\n")
            else:
                print("0) Quit\n1) Add Transaction\n2) View balance\n3) Print Chain\n4) Print Pending Transactions\n5) Print Current Stakers\n6) Time since last epoch\n7) Send Files\n8) Download Files\n")

            ch= await asyncio._get_running_loop().run_in_executor(
                None, input, "Enter Your Choice: "
            )
            try:
                ch=int(ch)
            except:
                print("\nPlease enter a valid number!!!\n")

            if ch != 0 and not self.ready.is_set():
                print("Synchronising the blockchain; please wait.")
                continue

            if ch==1:
                rec = await asyncio._get_running_loop().run_in_executor(
                    None, input, "\nEnter Receiver's Name or Public Key: "
                )

                if rec == "deploy":
                    contract_code = await asyncio.to_thread(get_contract_code_from_notepad)
                    if not contract_code:
                        print("Contract code field is empty")
                        continue
                    gas_used = len(contract_code)//10 + BASE_DEPLOY_COST
                    amount = round(gas_used * GAS_PRICE, 6)
                    payload = [contract_code, amount]

                    if amount<=Chain.instance.calc_balance(self.wallet.public_key_pem, self.mem_pool, list(self.current_stakes)):
                        await self.create_and_broadcast_tx(rec, payload)
                    else:
                        print("Insufficient Account Balance")
                elif rec == "invoke":
                    contract_id = await asyncio._get_running_loop().run_in_executor(
                        None, input, "\nEnter Contract Id: "
                    )
                    if contract_id not in self.contractsDB.contracts:
                        print("No such contract found...")
                        continue

                    func_name = await asyncio._get_running_loop().run_in_executor(
                        None, input, "\nEnter Function Name: "
                    )

                    args = []
                    loop = asyncio.get_running_loop()
                    arg_number = 1
                    while True:
                        arg = await loop.run_in_executor(None, input, f"Enter argument {arg_number} (or \\q to finish): ")
                        if arg.strip() == "\\q":
                            break
                        try:
                            parsed_arg = ast.literal_eval(arg)
                        except Exception:
                            parsed_arg = arg
                        args.append(parsed_arg)
                        arg_number += 1

                    response = await asyncio.to_thread(self.run_contract, [contract_id, func_name, args])
                    if(response["error"] != None):
                        print("Error: ", response["error"])
                        continue
                    state = response["state"]
                    gas_used = response["gas_used"]
                    amount = round(gas_used * GAS_PRICE, 6)

                    payload = [contract_id, func_name, args, state, amount]

                    if amount<=Chain.instance.calc_balance(self.wallet.public_key_pem, self.mem_pool, list(self.current_stakes)):
                        await self.create_and_broadcast_tx(rec, payload)
                    else:
                        print("Insufficient Account Balance")
                else:
                    amt= await asyncio._get_running_loop().run_in_executor(
                        None, input, "\nEnter Amount to send: "
                    )

                    receiver_public_key = self.name_to_public_key_dict.get(rec.lower().strip())

                    if receiver_public_key is None:
                        rec_split = rec.split("\\n")
                        rec_refined = "\n".join(rec_split)
                        exist = 0
                        for (nme, pk) in self.name_to_public_key_dict.items():
                            if pk == rec_refined:
                                receiver_public_key = pk
                                exist = 1
                                break
                        if exist == 0:
                            print("No person available in directory with provided name or public key...")
                            continue
                    
                    try:
                        amt=float(amt)
                    except ValueError:
                        print("Amount must be a number")
                        continue

                    if(amt<=0):
                        print("\nAmount must be positive\n")
                        continue

                    if amt<=Chain.instance.calc_balance(self.wallet.public_key_pem, self.mem_pool, list(self.current_stakes)):
                        await self.create_and_broadcast_tx(receiver_public_key, amt)
                    else:
                        print("Insufficient Account Balance")
            
            elif ch==2:
                print("Account Balance =",Chain.instance.calc_balance(self.wallet.public_key_pem, self.mem_pool, list(self.current_stakes)))

            elif ch==3:
                i=0
                # We print all the blocks
                if(not Chain.instance):
                    print("\nChain hasn't been initialized yet\n")
                    continue
                for block in Chain.instance.chain:
                    print(f"block{i}: {block}\n")
                    i+=1

            elif ch==4:
                i=0
                for transaction in self.mem_pool:
                    print(f"transaction{i}: {transaction}\n\n")
                    i+=1

            elif ch==5:
                async with self.curr_stakers_condition:
                    print("\n")
                    for key in self.current_stakers:
                        print(f"{key}:{self.current_stakers[key]}\n")
                    print("\n")

            elif ch==6:
                print(f"\n{(datetime.now()-self.last_epoch_end_ts).seconds}\n")

            elif ch==7:
                desc= await asyncio._get_running_loop().run_in_executor(
                    None, input, "\nEnter description of file: "
                )
                path= await asyncio._get_running_loop().run_in_executor(
                    None, input, "\nEnter path of file: "
                )
                pkt=await self.uploadFile(desc, path)
                if pkt:
                    await self.broadcast_message(pkt)

            elif ch==8:
                cid= await asyncio._get_running_loop().run_in_executor(
                    None, input, "\nEnter cid of file: "
                )
                path= await asyncio._get_running_loop().run_in_executor(
                    None, input, "\nEnter path to download the file: "
                )
                await asyncio.to_thread(download_ipfs_file_subprocess, cid, path, self.env)

            elif ch==9:
                try:
                    amt = await asyncio.to_thread(input, "New total stake (whole coins; may only increase): ")
                    txid = await self.send_stake_announcements(int(amt))
                    print("Stake update submitted; it takes effect after confirmation:", txid)
                except ValueError as exc:
                    print(exc)

            elif ch==0:
                print("Quitting...")
                break
    
    async def uploadFile(self, desc: str, path:str):
        file_path=Path(path)
        if(not file_path.is_file()):
            print("\nFile doesn't exist\n")
            return

        if not self.daemon_process: 
            await asyncio.to_thread(self.init_repo)
            await asyncio.to_thread(self.configure_ports)
            await asyncio.to_thread(self.start_daemon)
        
        cid, name = await asyncio.to_thread(addToIpfs, path, self.env)
        if(not(cid and name)):
            return
        
        print(f"\nNew File Created : {cid}\n")
        pkt={
            "type":"file",
            "id":str(uuid.uuid4()),
            "desc":desc,
            "cid":cid
        }
        
        self.seen_message_ids.add(pkt["id"])
        async with self.file_hashes_lock:
            self.file_hashes[cid]=desc
        return pkt

    def init_repo(self):
        """
            Creates a ipfs repo of name ending in ipfs_port_no eg ipfs_5000 
        """
        if not self.repo_path.exists():
            subprocess.run(["ipfs", "init"], env=self.env, check=True)
            print("\nIPFS repo created\n")

    def configure_ports(self):
        subprocess.run(["ipfs", "config", "Addresses.API", f"/ip4/127.0.0.1/tcp/{self.ipfs_port}"], env=self.env, check=True)
        subprocess.run(["ipfs", "config", "Addresses.Gateway", f"/ip4/127.0.0.1/tcp/{self.gateway_port}"], env=self.env, check=True)
        subprocess.run([
            "ipfs", "config", "Addresses.Swarm", "--json",
            f'["/ip4/127.0.0.1/tcp/{self.swarm_tcp}", "/ip4/127.0.0.1/udp/{self.swarm_udp}/quic"]'
        ], env=self.env, check=True)
        print("\nConfigured Ports\n")

    def start_daemon(self):
        self.daemon_process= subprocess.Popen(["ipfs", "daemon"], env=self.env)
        print("\nIPFS Daemon Started\n")

    def stop_daemon(self):
        if self.daemon_process:
            self.daemon_process.terminate()
            self.daemon_process.wait()

    async def connect_to_peer(self, host, port):
        endpoint = normalize_endpoint((host, port))
        if (endpoint == (self.host, self.port) or endpoint in self.outbound_peers or
                endpoint in self.connecting_peers or
                len(self.outbound_peers) + len(self.connecting_peers) >= MAX_CONNECTIONS):
            return
        self.connecting_peers.add(endpoint)
        websocket = None
        try:
            websocket = await websockets.connect(f"ws://{endpoint[0]}:{endpoint[1]}",
                                                 max_size=2**20, open_timeout=5)
            self.client_connections.add(websocket)
            self.outbound_peers.add(endpoint)
            self.have_sent_peer_info[websocket] = False
            await websocket.send(self.encode_packet({"type": "ping", "id": str(uuid.uuid4())}))
            async for raw in websocket:
                try:
                    msg = json.loads(raw, parse_constant=self.reject_constant)
                except (ValueError, TypeError):
                    self.rejected_messages += 1
                    continue
                await self.handle_messages(websocket, msg)
        except (OSError, websockets.exceptions.WebSocketException, asyncio.TimeoutError) as exc:
            self.record_event("connection", f"{endpoint[0]}:{endpoint[1]}: {str(exc)[:80]}")
        finally:
            self.connecting_peers.discard(endpoint)
            self.outbound_peers.discard(endpoint)
            self.client_connections.discard(websocket)
            self.connected_identities.pop(websocket, None)
            self.got_pong.pop(websocket, None)
            self.have_sent_peer_info.pop(websocket, None)
            if websocket:
                await websocket.close()

    async def discover_peers(self):
        while not self.stop_event.is_set():
            for endpoint in list(self.known_peers):
                if len(self.outbound_peers) + len(self.connecting_peers) >= MAX_CONNECTIONS:
                    break
                if endpoint not in self.outbound_peers and endpoint not in self.connecting_peers:
                    task = asyncio.create_task(self.connect_to_peer(*endpoint))
                    self.connection_tasks.add(task)
                    task.add_done_callback(self.connection_tasks.discard)
                    await asyncio.sleep(0)
            await asyncio.sleep(3)

    async def gossip_peer_sampler(self):
        """
            Every 60s, drops one existing peer and connects to one new peer.
        """
        while True:
            await asyncio.sleep(60)
            if len(self.known_peers) <= len(self.outbound_peers) or len(self.outbound_peers) < MAX_CONNECTIONS:
                continue  # Nothing to swap

            # Disconnect one random client connection
            to_drop = get_random_element(self.client_connections)
            if to_drop:
                print(f"Gossip Sampling: Disconnecting {to_drop.remote_address}")
                self.client_connections.discard(to_drop)
                normalized_endpoint = normalize_endpoint((to_drop.remote_address[0], to_drop.remote_address[1]))
                self.outbound_peers.discard(normalized_endpoint)
                self.got_pong.pop(to_drop, None)
                self.have_sent_peer_info.pop(to_drop, None)
                await to_drop.close()
                await to_drop.wait_closed()

            # Connect to a new peer (not already connected)
            potential_peers = {
                endpoint for endpoint in self.known_peers
                if endpoint not in self.outbound_peers and endpoint != (self.host, self.port)
            }

            if potential_peers:
                new_peer = get_random_element(potential_peers)
                if new_peer:
                    print(f"Gossip Sampling: Connecting to new peer {new_peer}")
                    asyncio.create_task(self.connect_to_peer(*new_peer))

    async def send_stake_announcements(self, amt):
        if type(amt) is not int or amt <= 0:
            raise ValueError("Stake must be a positive whole number; stakes stay locked in this simulation")
        return await self.create_and_broadcast_tx("stake", amt)

    async def restart_epoch(self):
        while not self.stop_event.is_set():
            if self.ready.is_set() and self.staker:
                slot = slot_at(self.chain.chain)
                if slot > self.last_attempt_slot and slot > self.chain.lastBlock.slot:
                    self.last_attempt_slot = slot
                    await self.create_blocks(0)
            await asyncio.sleep(1)

    async def create_blocks(self, delay):
        await asyncio.sleep(delay)
        if not self.staker or not self.ready.is_set():
            return
        async with self.state_lock:
            slot = slot_at(self.chain.chain)
            if slot <= self.chain.lastBlock.slot:
                return
            stakes = registered_stakes(self.chain.chain)
            registry = {s.staker: s.amt for s in stakes}
            mine = registry.get(self.wallet.public_key_pem, 0)
            seed = self.chain.epoch_seed(slot)
            if not lottery_wins(self.wallet.public_key_pem, seed, mine, sum(registry.values())):
                self.record_event("lottery", f"No winning ticket in slot {slot}")
                return
            evidence = [p for p in self.pending_evidence.values()
                        if valid_evidence(p, self.chain.chain) and evidence_id(p) not in used_evidence(self.chain.chain)][:16]
            transactions = []
            for tx in self.mem_pool:
                if valid_transaction(tx, self.chain.chain, transactions, evidence):
                    if tx.receiver in ("deploy", "invoke") and not await self.validate_contract_batch(transactions + [tx]):
                        self.record_event("rejected", f"Dropped stale contract transaction {tx.id[:12]}")
                        continue
                    transactions.append(tx)
            self.mem_pool = transactions
            block = Block(self.chain.lastBlock.hash, transactions)
            block.epoch_seconds = self.chain.chain[0].epoch_seconds
            block.creator, block.staked_amt, block.stakers = self.wallet.public_key_pem, mine, stakes
            block.slot, block.seed = slot, seed
            block.vrf_proof = self.wallet.private_key.sign_deterministic(seed.encode(), hashfunc=hashlib.sha256)
            block.files = dict(self.file_hashes)
            block.slash_evidence = evidence
            sign_block(block, self.wallet.private_key)
            if not await asyncio.to_thread(self.chain.isValidBlock, block):
                self.record_event("rejected", "Local candidate failed the shared validator")
                return
            await self.publish_block(block)
                                                       
    async def find_longest_chain(self):
        while not self.stop_event.is_set():
            await self.broadcast_message({"type": "chain_request", "id": str(uuid.uuid4())})
            await asyncio.sleep(5)

    def calculate_contract_id(self, sender, timestamp):
        data = f"{sender}:{timestamp}"
        hash_object = hashlib.sha256(data.encode('utf-8'))
        return hash_object.hexdigest()
        
    def deploy_contract(self, transaction):
        sender = transaction.sender
        timestamp = transaction.ts
        code = transaction.payload[0]
        contract_id = self.calculate_contract_id(sender, timestamp)
        self.contractsDB.store_contract(contract_id, code)
        print("Contract deployed with id: ", contract_id)

    def run_contract(self, payload):
        contract_id, func_name, args = payload[0], payload[1], payload[2]
        code = self.contractsDB.get_contract(contract_id)
        if code is None:
            raise ValueError(f"Contract '{contract_id}' not found.")

        state = self.get_contract_state(contract_id)
        executor = SecureContractExecutor(code)
        response = executor.run(func_name, args, state)

        return response

    async def start(self, bootstrap_host=None, bootstrap_port=None, signalling_url=None,
                    create_room=False, web_port=None, no_cli=False):
        self.connection_tasks = set()
        tasks = []
        server = await websockets.serve(self.handle_connections, self.host, self.port, max_size=2**20)
        try:
            if signalling_url:
                if create_room and not self.chain:
                    self.chain = Chain(publicKey=self.wallet.public_key_pem, privatekey=self.wallet.private_key, epoch_seconds=self.epoch_seconds)
                    self.network_id = self.chain.chain[0].hash
                from signalling_server import SignallingClient
                self.signalling = SignallingClient(self, signalling_url, create_room)
                await self.signalling.connect()
                tasks.append(asyncio.create_task(self.signalling.listen()))
            elif not bootstrap_host and not self.chain:
                self.chain = Chain(publicKey=self.wallet.public_key_pem, privatekey=self.wallet.private_key, epoch_seconds=self.epoch_seconds)
                self.network_id = self.chain.chain[0].hash
            if self.chain:
                self.refresh_state()
            if bootstrap_host:
                task = asyncio.create_task(self.connect_to_peer(bootstrap_host, bootstrap_port))
                self.connection_tasks.add(task)
                task.add_done_callback(self.connection_tasks.discard)
            tasks += [asyncio.create_task(self.restart_epoch()), asyncio.create_task(self.find_longest_chain()),
                      asyncio.create_task(self.discover_peers())]
            if web_port:
                from web_app import NodeWebServer
                self.web_server = NodeWebServer(self, asyncio.get_running_loop(), web_port)
                self.web_server.start()
                print(f"Web interface: http://127.0.0.1:{web_port}")
            if no_cli:
                await self.stop_event.wait()
            else:
                await self.user_input_handler()
        finally:
            self.stop_event.set()
            for task in tasks + list(self.connection_tasks):
                task.cancel()
            await asyncio.gather(*tasks, *list(self.connection_tasks), return_exceptions=True)
            if signalling_url and hasattr(self, "signalling"):
                await self.signalling.close()
            for ws in list(self.client_connections | self.server_connections):
                await ws.close()
            server.close()
            await server.wait_closed()
            if self.web_server:
                await asyncio.to_thread(self.web_server.stop)
            await asyncio.to_thread(self.stop_daemon)

    async def process_message(self, websocket, msg):
        kind = msg.get("type")
        if kind == "ping":
            await websocket.send(self.encode_packet({"type": "pong", "id": str(uuid.uuid4())}))
        elif kind == "pong":
            self.got_pong[websocket] = True
            if not self.have_sent_peer_info.get(websocket, True):
                self.have_sent_peer_info[websocket] = True
                await self.send_peer_info(websocket)
        elif kind in {"peer_info", "add_peer", "new_peer"}:
            data = msg["data"]
            key = data["public_key"]
            if not valid_public_key(key):
                raise ValueError("Invalid peer public key")
            VerifyingKey.from_pem(key).verify(base64.b64decode(msg["identity_sign"], validate=True),
                canonical([self.room_id, self.network_id, data]).encode(), hashfunc=hashlib.sha256)
            self.register_peer(data)
            if kind != "new_peer":
                self.connected_identities[websocket] = key
                await self.send_known_peers(websocket)
                packet = dict(msg, type="new_peer", id=str(uuid.uuid4()))
                await self.broadcast_message(packet)
            else:
                await self.broadcast_message(msg)
        elif kind == "known_peers":
            peers = msg["peers"]
            if not isinstance(peers, list) or len(peers) > 256:
                raise ValueError("Too many peers")
            for data in peers:
                self.register_peer(data)
            await websocket.send(self.encode_packet({"type": "chain_request", "id": str(uuid.uuid4())}))
        elif kind == "chain_request":
            if self.chain:
                await websocket.send(self.encode_packet({"type": "chain", "id": str(uuid.uuid4()),
                    "chain": self.chain.to_block_dict_list()}))
        elif kind == "chain":
            data = msg.get("chain")
            if not isinstance(data, list) or not 0 < len(data) <= 5000:
                raise ValueError("Invalid chain size")
            blocks = [self.block_dict_to_block(item) for item in data]
            if blocks[0].hash != self.network_id:
                raise ValueError("Invalid or foreign chain")
            old = list(self.chain.chain) if self.chain else []
            if not self.chain:
                self.chain = await asyncio.to_thread(Chain, blockList=blocks)
            else:
                blocks = await asyncio.to_thread(self.chain.validated_candidate, blocks)
                if blocks is None:
                    raise ValueError("Invalid chain")
                for left, right in zip(old, blocks):
                    if (left.hash != right.hash and left.creator == right.creator and
                            left.prevHash == right.prevHash and left.slot == right.slot):
                        await self.verify_and_slash(left, right, 0, blocks)
                if chain_rank(blocks) <= chain_rank(self.chain.chain):
                    return
                self.chain.chain = blocks
            self.refresh_state(old)
            self.record_event("sync", f"Accepted history with {len(self.chain.chain)} blocks")
        elif kind == "new_tx":
            self.require_ready()
            data = json.loads(msg["transaction"], parse_constant=self.reject_constant)
            tx = Transaction(data["payload"], data["sender"], data["receiver"], data["id"], data["ts"], network=data.get("network"))
            if msg.get("sender_pem") != tx.sender:
                raise ValueError("Signature key does not match transaction sender")
            tx.sign = base64.b64decode(msg["sign"], validate=True)
            if not valid_transaction(tx, self.chain.chain, self.mem_pool):
                raise ValueError("Invalid, duplicate, or unaffordable transaction")
            if not await self.validate_contract_batch(self.mem_pool + [tx]):
                raise ValueError("Invalid contract result")
            if len(self.mem_pool) >= 1000:
                raise ValueError("Pending transaction pool is full")
            self.mem_pool.append(tx)
            self.record_event("transaction", "Received " + tx.id[:12])
            await self.broadcast_message(msg)
        elif kind == "new_block":
            self.require_ready()
            data = dict(msg["block"])
            data["sign"] = msg.get("sign", data.get("sign"))
            block = self.block_dict_to_block(data)
            if any(b.hash == block.hash for b in self.chain.chain):
                return
            parent = next((i for i, b in enumerate(self.chain.chain) if b.hash == block.prevHash), None)
            if parent is None:
                await websocket.send(self.encode_packet({"type": "chain_request", "id": str(uuid.uuid4())}))
                return
            prefix = self.chain.chain[:parent + 1]
            if not await asyncio.to_thread(valid_block, block, prefix):
                raise ValueError("Invalid block")
            if parent + 1 < len(self.chain.chain):
                previous = self.chain.chain[parent + 1]
                if previous.creator == block.creator and previous.slot == block.slot:
                    await self.verify_and_slash(previous, block, parent + 1, prefix + [block])
            candidate = prefix + [block]
            old = list(self.chain.chain)
            if chain_rank(candidate) > chain_rank(self.chain.chain):
                self.chain.chain = candidate
                self.refresh_state(old)
                self.record_event("block", f"Accepted slot {block.slot}")
            await self.broadcast_message(msg)
        elif kind == "slash_announcement":
            self.require_ready()
            proof = msg["evidence"]
            if not valid_evidence(proof, self.chain.chain):
                raise ValueError("Invalid or identical slashing evidence")
            key = evidence_id(proof)
            if key not in used_evidence(self.chain.chain) and key not in self.pending_evidence:
                if len(self.pending_evidence) >= 128:
                    raise ValueError("Evidence queue is full")
                self.pending_evidence[key] = proof
                self.record_event("evidence", "Verified conflicting signed blocks; penalty awaits confirmation")
                await self.broadcast_message(msg)
        elif kind == "file":
            cid, desc = msg["cid"], msg["desc"]
            if not isinstance(cid, str) or not 0 < len(cid) <= 256 or not isinstance(desc, str) or len(desc) > 500:
                raise ValueError("Invalid file announcement")
            if len(self.file_hashes) < 100 and not (self.chain and self.chain.cid_exists_in_chain(cid)):
                self.file_hashes[cid] = desc
                await self.broadcast_message(msg)
        else:
            raise ValueError("Unknown message type")

    async def publish_block(self, block):
        old = list(self.chain.chain)
        self.chain.chain.append(block)
        self.refresh_state(old)
        self.record_event("block", f"Created slot {block.slot}")
        packet = {"type": "new_block", "id": str(uuid.uuid4()),
                  "block": block.to_dict_with_stakers(), "sign": base64.b64encode(block.sign).decode()}
        self.seen_message_ids.add(packet["id"])
        await self.broadcast_message(packet)

    @staticmethod
    def reject_constant(value):
        raise ValueError("Non-finite JSON number: " + value)

    def encode_packet(self, packet):
        return json.dumps(dict(packet, protocol="pos-v2", room=self.room_id,
                               network=self.network_id), allow_nan=False)

    def record_event(self, kind, message):
        self.events.append({"time": datetime.now().isoformat(timespec="seconds"), "kind": kind, "message": message})

    def require_ready(self):
        if not self.ready.is_set() or not self.chain:
            raise ValueError("Node is still synchronising; wait for its blockchain")

    def register_peer(self, data):
        endpoint = normalize_endpoint((data["host"], data["port"]))
        if not 1 <= endpoint[1] <= 65535 or not valid_public_key(data["public_key"]):
            raise ValueError("Invalid peer address or identity")
        name = data["name"]
        if not isinstance(name, str) or not 0 < len(name) <= 40:
            raise ValueError("Invalid peer name")
        if endpoint == (self.host, self.port):
            return
        if len(self.known_peers) >= 255 and endpoint not in self.known_peers:
            return
        previous = self.known_peers.get(endpoint)
        if previous and previous[1] != data["public_key"]:
            raise ValueError("Peer identity changed at an existing endpoint")
        self.known_peers[endpoint] = (name, data["public_key"])
        lookup = name.lower()
        if lookup in self.name_to_public_key_dict and self.name_to_public_key_dict[lookup] != data["public_key"]:
            lookup += "-" + hashlib.sha256(data["public_key"].encode()).hexdigest()[:6]
        self.name_to_public_key_dict[lookup] = data["public_key"]
        if self.activate_disk_save == "y":
            self.save_known_peers_to_disk()

    async def validate_contract_batch(self, transactions):
        if not any(tx.receiver in ("deploy", "invoke") for tx in transactions):
            return True
        from smart_contract.validation import validate_contract_transactions
        return await asyncio.to_thread(validate_contract_transactions, list(self.chain.chain), list(transactions))

    def refresh_state(self, old_chain=None):
        self.network_id = self.chain.chain[0].hash
        self.epoch_seconds = self.chain.chain[0].epoch_seconds
        self.current_stakes = set(registered_stakes(self.chain.chain))
        self.current_stakers = {s.staker: s.amt for s in self.current_stakes}
        self.staked_amt = self.current_stakers.get(self.wallet.public_key_pem, 0)
        candidates = list(self.mem_pool)
        present = {t.id for b in self.chain.chain for t in b.transactions}
        seen = {t.id for t in candidates}
        for block in old_chain or []:
            for tx in block.transactions:
                if tx.sender != "Genesis" and tx.id not in present and tx.id not in seen:
                    candidates.append(tx)
                    seen.add(tx.id)
        self.mem_pool = []
        for tx in candidates[:1000]:
            if valid_transaction(tx, self.chain.chain, self.mem_pool):
                self.mem_pool.append(tx)
        included = used_evidence(self.chain.chain)
        self.pending_evidence = {key: value for key, value in self.pending_evidence.items()
                                 if key not in included and valid_evidence(value, self.chain.chain)}
        self.contractsDB.contracts.clear()
        for block in self.chain.chain:
            for tx in block.transactions:
                if tx.receiver == "deploy":
                    self.contractsDB.store_contract(self.calculate_contract_id(tx.sender, tx.ts), tx.payload[0])
            for cid in block.files:
                self.file_hashes.pop(cid, None)
        self.last_epoch_end_ts = datetime.fromtimestamp(self.chain.lastBlock.ts / 1000)
        self.ready.set()
        if self.activate_disk_save == "y":
            self.save_chain_to_disk()

    async def snapshot(self):
        async with self.state_lock:
            blocks = self.chain.chain if self.chain else []
            balances, stakes = ledger_state(blocks) if blocks else ({}, {})
            key = self.wallet.public_key_pem
            locked = stakes[key].amt if key in stakes else 0
            active_connections = set(self.connected_identities.values())
            return {
                "name": self.name, "host": self.host, "port": self.port, "room": self.room_id,
                "ready": self.ready.is_set(), "network": self.network_id, "public_key": key,
                "staker": self.staker, "malicious": getattr(self, "malicious", False),
                "signalling_connected": self.signalling_connected,
                "balance": balances.get(key, 0) / COIN_UNITS, "stake": locked,
                "available": self.chain.calc_balance(key, self.mem_pool) if blocks else 0,
                "height": len(blocks) - 1, "weight": weight_of_chain(blocks),
                "slot": slot_at(blocks) if blocks else 0, "epoch_seconds": self.epoch_seconds,
                "next_slot_seconds": max(0, self.epoch_seconds - ((int(time.time() * 1000) - blocks[0].ts) / 1000) % self.epoch_seconds) if blocks else 0,
                "connections": len(self.client_connections | self.server_connections),
                "rejected_messages": self.rejected_messages,
                "pending_evidence": len(self.pending_evidence),
                "confirmed_penalties": sum(len(b.slash_evidence) for b in blocks),
                "validators": [{"public_key": k, "stake": v.amt} for k, v in stakes.items()],
                "peers": [{"host": h, "port": p, "name": n, "public_key": pk,
                           "connected": pk in active_connections}
                          for (h, p), (n, pk) in self.known_peers.items()],
                "pending": [t.to_dict() for t in self.mem_pool],
                "contracts": list(self.contractsDB.contracts),
                "blocks": [{"height": i, "hash": b.hash, "parent": b.prevHash, "slot": b.slot,
                            "creator": b.creator, "transactions": len(b.transactions),
                            "stake": b.staked_amt, "penalties": len(b.slash_evidence), "timestamp": b.ts}
                           for i, b in list(enumerate(blocks))[-100:]],
                "events": list(self.events),
            }
