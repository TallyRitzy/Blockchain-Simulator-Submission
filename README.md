# Blockchain App
A web and terminal blockchain implementation in Python from scratch

## PoS v2: corrected consensus, rooms and browser controls

The PoS path now uses shared validation for locally created blocks, received blocks and downloaded history. It includes room discovery, a local dashboard, reproducible slashing evidence and isolated node storage. The original modules, main classes, terminal menu and PoW/PoA engines are retained.

Start with [the beginner walkthrough](docs/QUICK_START.md). Read [Changes and defence](docs/CHANGES_AND_DEFENCE.md) for each change's reason, implementation, tests and evaluation questions.

After installing the existing requirements, open three terminals in this repository and run one command in each:

```powershell
.\venv\Scripts\python.exe signalling_server.py
```

```powershell
.\venv\Scripts\python.exe start_peer.py --port 5000 --name Alice --room classroom --create-room --web-port 8000 --epoch-seconds 10 --save
```

```powershell
.\venv\Scripts\python.exe start_peer.py --port 5001 --name Bob --room classroom --web-port 8001 --save
```

Open [Alice](http://127.0.0.1:8000) and [Bob](http://127.0.0.1:8001). Send Bob 15 coins, wait for confirmation, then register a total stake of 5 on Bob's page. Coins are simulation values. Default rounds last 60 seconds; the creator's example uses 10 seconds for a demonstration. A round may have no winner or multiple winners.

For a later restart of either saved node, repeat its command with `--load`. Keep the same room and port. All participants must use this PoS v2 code; older PoS histories are incompatible and are left in their original storage location. Room IDs separate discovery but are not passwords. This is a local educational network with a publicly predictable stake lottery, not a production cryptocurrency or a true cryptographic VRF.

## Features
- Peer-to-Peer network with decentralized communication
- Public/private key-based account system
- Digital signature verification
- Selectable consensus mechanism - PoW, PoS, PoA
- Smart contract deployment
- IPFS integration
- Persistent storage
- Malicious node to test security
- Command line & web interface

## Contents
- [Theory](#theory)
- [About this project](#about-this-project)
- [How to run this project](#how-to-run-this-project)

## Theory
### What is blockchain?
A blockchain is a decentralized, distributed digital ledger where data is stored in blocks linked together in a chain
- A block is made of list of transactions
### Peer-to-Peer Network
Since, there is no central authority, network is formed in a peer-to-peer fashion.
### Consensus Mechanism
Blockchain involves transactions in a trustless environment. So there is need for a mechanism to ensure integrity of the chain. There comes the need of consensus mechanisms. Each consensus mechanism ensures integrity of the chain in their own way.
#### Proof of Work(PoW)
- Nodes compete to solve a cryptographic puzzle
- The winner gets to add the next block to the chain
#### Proof of Stake(PoS)
- Nodes with confirmed stake enter a verifiable, stake-weighted hash lottery.
- Eligible nodes may propose a block; every receiver independently checks it.
#### Proof of Authority(PoA)
- A limited set of trusted nodes(authorities) validate and create new blocks
### Smart Contracts
- A smart contract is like a digital agreement written in code
- It sits on the blockchain and runs automatically when certain rules are met
### IPFS
Blockchains are not designed for storing large amount of data. That's where IPFS comes in.
- It's a decentralized file storage system
- Each file is identified by its content
- A unique hash called CID(Content Identifiers) is generated based on the content(Files with same content will have same CID)
- IPFS uses a Distributed Hash Table(DHT), similar to BitTorrent's Kademlia DHT
- When you request a CID, your node queries the DHT to ask "Which peers are providing this CID?"
- Nodes that have previously announced that CID to the DHT will be returned as providers
- Your node then directly connects to those providers via IPFS's peer-to-peer transport protocols(libp2p)

## About this project
### Basic Structure
Each **node** contains its own set of
- Known peers list (members of the network)
- Client connections (connection established by your node to other nodes)
- Server connections (connection established by other nodes to your node)
- Wallet (acts as your account in the network)
- Transaction pool (contains all transactions pending to be mined)
- Chain (personal copy of the blockchain)

Each **account** contains
- Private key
- Public key

**Transactions** are of 4 types in PoS v2

- **Stake Transaction** - Set a funded, positive whole-coin total stake. It takes effect when included in a block. Stake stays locked and may only increase in this simplified model.

- **Coin Transaction** - To transfer money
  - Timestamp
  - Public key of the sender
  - Public key of the receiver
  - Transaction amount
- **Deploy Transaction** - To deploy contract
  - Timestamp
  - Public key of the sender
  - Contract code
  - Deploy charge

- **Invoke Transaction** - To invoke contract
  - Timestamp  
  - Public key of the sender  
  - Contract ID  
  - Function name  
  - Arguments  
  - New state  
  - Invoke charge  

Each **block** contains
- Timestamp
- List of transactions
- Hash of previous block
- Current block hash
- Miner info
- List of files
### Handshake Protocol
- Client: Sends ping
- Server: Receives ping &rightarrow; sends pong
- Client: Receives pong &rightarrow; Sends peer info (information about itself)
- Server: Receives peer info &rightarrow; adds it to its known peers (if not already present) &rightarrow; sends back known_peers (list of all nodes it knows)
- Client: Receives known_peers &rightarrow; adds new peers to its own known_peers &rightarrow; requests the chain
- Server: Receives chain request &rightarrow; sends its current chain
- Client: Receives the chain and validates it. PoS v2 accepts only the same genesis identity, compares total chain weight, and breaks equal-weight ties by tip hash. The legacy engines have their own selection rules.

In room mode, a separate WebSocket signalling server first checks a signed registration and returns the room's peer directory and genesis identity. Nodes then use direct peer connections for the handshake and blockchain traffic. A directory outage does not stop existing peer connections.
### Peer-to-Peer Network
PoS v2 discovers room members and maintains up to 8 outgoing and 32 incoming connections per node. Connection attempts and remembered message IDs are bounded. Small rooms connect directly; the dashboard distinguishes discovered peers from connected peers. A pair of nodes can have an incoming and an outgoing socket, so the connection count can exceed the peer count. The old gossip sampler remains available in the legacy code but is not part of the v2 startup loop.

**Implemented Using:** python websockets, asyncio
### Consensus Mechanism
Users can select their prefered consensus mechanism from the list of three available
#### Proof of Work(PoW)
- A new block is mined every 30 secs, if there are pending transactions in the transaction pool
- Mining nodes collect transactions into a block
- Node that first finds a valid hash gets the chance to mine
- Difficulty is set to 5. That means, a valid hash is the one which starts with five zeroes
- Nonce is incremented until finding a valid hash
- Once mined, the block is broadcasted to the network
- All nodes validate the block before adding it to their chain
#### Proof of Stake(PoS)
- The signed genesis block fixes the start time and slot duration. All nodes derive the same numbered slots; their system clocks must be reasonably aligned.
- Genesis retains the original 50-coin grant and 6-coin reward. Its creator starts with 10 of those coins locked to bootstrap consensus. Other validators must receive coins and confirm a signed stake transaction.
- The lottery seed binds the genesis identity, parent hash and slot. The ticket hashes that seed with the public key. A ticket wins when `ticket * total_stake < own_stake * 2**256`. Retrying an ECDSA signature cannot change this ticket.
- The retained `vrf_proof` field proves key ownership over the seed. It is not a standard cryptographic VRF. Tickets are publicly predictable; randomness hardening is outside this educational model.
- A block commits to the exact validator registry derived from earlier confirmed blocks. Its signature covers its hash and consensus header. Balances use integer millionths of a coin, and pending transactions are checked in order.
- Empty blocks are allowed. No winning ticket means a skipped slot; competing winners produce forks. Valid chains are ranked by the sum of their non-genesis blocks' registered total stake, then tip hash.
- Two different signed block hashes from the same creator, parent and slot provide evidence of double signing. A later block includes that evidence and burns the offending locked stake. Replaying history reproduces the penalty; old blocks and their transactions are not erased.
- Stake withdrawals, checkpoint finality, protection against majority-stake attacks and production-grade randomness are not implemented. Accepted balances follow the selected chain and may change after a fork.
#### Proof of Authority(PoA)
- Initially, admin, the one who started the chain is the only miner
- Admin can add or remove miners
- Each block can be mined only by the assigned miner
- If that miner is inactive, mining will be handed over to the next miner
### Smart Contracts
In this project
- Smart Contracts are written in python
- Users can write their own smart contracts and deploy
- Users can also invoke the deployed contract using their deployed address
- They run in a RestrictedPython child process with time, memory and operation limits. Nodes re-execute calls to check reported state and gas charges. This is an educational restriction layer, not a proven sandbox for hostile public deployments.

**Implemented Using:** RestrictedPython, multiprocessing
### IPFS
IPFS is integrated as a wrapper for the existing IPFS network. IPFS hashes of the user uploaded files are stored in the blocks. Other users can use this to download the file.  

**Implemented Using:** IPFS
### Persistent Storage
Persistent storage is implemented to enable nodes to reconnect to the network using there previous network data

PoS v2 uses `storage/pos/v2/<room-hash>/<peer-port>/`. `--save` writes the wallet, chain and peers; `--load` reads them. Writes use a temporary file followed by atomic replacement. Private keys remain plaintext local files, so do not share the storage directory. Neither flag is enabled implicitly.
### Malicious Node
- To test the security and robustness of our networks we created a malicious node that attempts
    1. Generate invalid transactions (amt>account balance or amount<=0)
    2. Double Sign
- We have tested our blockchain networks using this malicious nodes to verify that our protocols are working and that our network is functional

## How to run this project
### Prerequisites
- Python 3.12+ (validated with Python 3.12.14). Existing nested f-strings require [the Python 3.12 syntax rules](https://docs.python.org/3/whatsnew/3.12.html#pep-701-syntactic-formalization-of-f-strings).
- `pip` (python package manager)
- `venv` (for creating virtual environment)
### Installation & Setup
Clone project into your local machine
```bash
git clone https://github.com/TatHack-Tathva/Blockchain-Simulation.git
```
Enter into the project folder
```bash
cd Blockchain-Simulation
```
Create and activate virtual environment
```bash
python -m venv venv
.\venv\Scripts\python.exe --version
```
Install dependencies
```bash
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```
### Terminal App
Start terminal app
```bash
.\venv\Scripts\python.exe start_peer.py
```

sample:
  host: localhost
  port: 5000
  name: john

The original menu retains bootstrap mode and the legacy PoW/PoA choices. For room discovery and the dashboard, use the explicit commands above. Add `--cli` for terminal controls alongside a room node's web page. IPFS is optional, requires a separately installed `ipfs` executable, and is accessed through the terminal menu. It is not needed for the required PoS/room/dashboard demonstration.

### Verification

```powershell
.\venv\Scripts\python.exe -B -m unittest discover -s tests -v
.\venv\Scripts\python.exe -B -m tests.integration_demo
```

The integration test starts real local processes on temporary ports, uses temporary saved data, and cleans up when it finishes. It covers discovery, transfers, staking, malicious double signing, overspending rejection, restart, directory outage/recovery, separate rooms and smart contracts. See the defence guide for test scope and remaining limitations. The legacy PoW/PoA engines and external IPFS daemon were not certified by these tests.

## Authors

**Rahan M**
[GitHub](https://github.com/Rahan-M) | [LinkedIn](https://www.linkedin.com/in/rahan-m-077a32254?utm_source=share&utm_campaign=share_via&utm_content=profile&utm_medium=android_app)

**Jefin Joji**
[GitHub](https://github.com/JefinCodes) | [LinkedIn](https://www.linkedin.com/in/jefin-joji-659354313?utm_source=share&utm_campaign=share_via&utm_content=profile&utm_medium=android_app)
