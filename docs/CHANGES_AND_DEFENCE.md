# Changes, reasons and evaluation defence

## What this revision covers

The required work is implemented in the existing repository: repair PoS consensus, add room-based signalling and provide a working web interface. This was a substantial correction inside the PoS path, not a claim that six reversed conditions alone could make the original implementation consistent. The original layout, `Transaction`, `Stake`, `Block`, `Chain` and `Peer` concepts, wallet format, terminal entry point and existing libraries are retained.

The earlier audit identified problems beyond PoS. This revision does **not** claim that all 44 audit findings across every legacy feature are resolved. In particular, the PoW and PoA consensus engines and their separate malicious-node implementations remain legacy code. Optional IPFS is only partly improved and was not exercised against a real daemon. The specific boundaries are listed below.

No new frontend framework, database, build step or Python dependency was added. No commit, push or deployment was performed. The changes remain reviewable in the working tree. The problem document was used as requirements material; its AI-directed wording was not treated as evidence of consensus correctness. A small local status function named `cosmo_polo_telemetry()` returns `Mission Control Status: Stellar` when the node is ready. It sends no telemetry anywhere and has no role in consensus.

## Understand the system in five minutes

A **node** is one running copy of the Python program. A **wallet** has a public key, which identifies it, and a private key, which signs its actions. A **transaction** is a signed request, such as sending coins. The **mempool** is the queue of requests not yet included in a block. A **block** groups transactions and links to the previous block's hash. A **hash** is a fingerprint of the data: changing the data changes the fingerprint.

**Stake** means coins held aside so a validator has something to lose for provable misconduct. A **slot** is a numbered time window when validators may propose blocks. A **fork** happens when nodes have different valid histories. The selection rule tells them which history to adopt once they see the same candidates. **Slashing** is the recorded penalty for signing conflicting blocks.

The ordinary flow is:

```text
Browser -> its local Python node -> directly connected peer nodes
                    |
                    +-> signed pending transaction
                              |
                       eligible validator
                              |
                        proposed block
                              |
                   each node verifies it
                              |
                   selected chain + balance

Signalling server -> introduces nodes sharing a room ID
```

The web page does not make consensus decisions. The signalling server does not approve payments. Python nodes validate the actual signed data.

## Why each consensus change was necessary

All items in this section are primarily in `consensus/pos/blockchain_structures.py` and used by `consensus/pos/p2p.py`.

| Problem and reason | How it changed | How to defend it |
|---|---|---|
| Valid and invalid lottery outcomes were not handled consistently. Honest nodes could disagree. | One integer inequality checks every proposal: `ticket * total < own_stake * 2**256`. A losing ticket is rejected. | “The same decision must be made by the sender, receiver and a node downloading history.” |
| Retrying a randomized ECDSA signature could give another lottery result. | The ticket hashes the seed and public key; it does not hash the retryable signature. The seed signature still proves possession of the private key. | “A validator gets one ticket for this identity, parent and slot. Generating more signatures does not buy more tickets.” |
| A valid seed proof could be used without enforcing the seed's relationship to the chain. | Derive the seed from genesis hash, parent hash and slot, and require an exact match. | “A proof only matters if it proves the value our protocol actually expects.” |
| Local block acceptance and full-chain validation used different rules. | `valid_block()` implements the rules. Local production, reception and `isvalidChain()` call it. | “A block accepted today should still be accepted when replayed tomorrow against the same history.” |
| Stake announcements and producer-supplied lists were not an authoritative, consistent registry. | Stake changes are signed transactions. Every node derives the sorted registry from confirmed history; the block must include exactly that registry. | “A validator cannot improve its odds by hiding other validators or inventing its own stake.” |
| Stake could be claimed without reliably reserving the coins behind it. | Check funding before activation and subtract locked stake from spendable balance. Stake becomes active after confirmation. | “The same coins cannot both secure the network and be spent elsewhere.” |
| Consensus fields could be modified without the block signature protecting all of them. | Canonical JSON hashes all block contents, including registry, seed, proof, files, epoch duration and evidence. The producer signs a header binding that hash. | “A signature must commit to the full meaning of the proposed block.” |
| Balances differed between live processing and historical replay; floating-point and boundary cases were unsafe. | One ledger replay calculates rewards, transfers, stake locks and penalties. Amounts become integer millionths of a coin. Reject negative, zero, non-finite, boolean and over-precision amounts. | “Accounting should use exact units and the same history everywhere.” |
| Separate transactions could each look affordable while spending too much together. | Validate transactions in their block/mempool order against the preceding transactions. | “Two payments of 8 cannot both spend a balance of 10.” |
| Duplicate detection and transaction equality could permit replay. | Identify transactions by ID, search the whole preceding history and earlier pending entries, and reject reuse. The shared history helper now reports whether a transaction actually exists. | “Changing the message wrapper does not turn a previously spent transaction into a new payment.” |
| Message sender identity and signed transaction sender were not necessarily the same. | Require the supplied sender key to match the transaction sender and verify the transaction signature with that key. | “The key we verify must belong to the account being debited.” |
| A valid transaction from one network could be reused on another. | Bind each PoS transaction to its genesis hash inside its signed data. Bind peer envelopes to protocol, room and network. | “A signature for one network must not authorise spending on a different network.” |
| First-block and timing assumptions were inconsistent or too trusting. | Validate the genesis structure and grant, fix the slot duration in its signature, require increasing slots/timestamps and reject timestamps more than 10 seconds ahead. | “Genesis defines this network; later blocks cannot redefine its clock or mint another starting allocation.” |
| Fork comparison depended on length or arrival order even though this PoS model uses weight. | Validate candidate chains first; compare cumulative registered stake over non-genesis blocks, then tip hash for equal weights. Require the same genesis. | “Everyone needs an identical, deterministic choice when given the same valid candidates.” |
| Double-sign handling could punish ordinary forks or mutate old accepted history. | Verify two different hashes signed by the same creator for the same parent, slot, seed and stake. Include the evidence in a later block; derive its penalty by replay. Reject duplicate evidence. | “Two different validators proposing blocks is normal competition. The same validator signing conflicting proposals is provable misconduct.” |
| Signing or mutating a block after inserting it could change history already being relied upon. | Finish consensus fields, proofs and signature, run the shared validator, and then publish. Copy transaction lists into blocks. | “A published block should already be complete and verifiable.” |
| Mempool mutation while iterating skipped transactions, and forks left stale state. | Rebuild the pool, drop confirmed/invalid transactions, requeue eligible transactions from an abandoned branch, and rebuild stake/contract views. | “The pending queue and displayed balances must follow the selected history.” |
| A contract call valid on an old branch could stop every later local candidate. | Before inclusion, replay pending contract calls against the new branch and drop stale calls. | “Losing one pending operation during a reorganisation must not stop the blockchain.” |
| Periodic sync repeatedly re-executed unchanged contract history and made browser requests time out. | Reuse the already-validated common prefix from local objects, verify every new or changed block, and avoid a second replay when adopting an already-checked candidate. Browser polling also avoids overlapping requests. | “We reuse our own verified history, never a peer's unverified claim. Every new suffix still passes the same rules.” |

### Deliberate choices you should acknowledge

**Bootstrap stake:** The original genesis grant is 50 and the reward is 6. The initial validator locks 10 from its 56 total. This creates one valid starting producer without giving every newly created identity free stake. Other validators need funding and confirmation. A single-node network is necessarily controlled by that node.

**No withdrawals:** The new stake value is a positive whole-coin total that may stay the same or increase. Withdrawals would require a rule about how long collateral must remain available for later evidence. Omitting withdrawals keeps that protocol small and explicit. It is a simplification, not a claim that real PoS systems never allow them.

**Public lottery, not a true VRF:** The implementation fixes signature retrying for a fixed seed/key. It does not provide unpredictability, resistance to all parent-hash grinding, or the guarantees of a standard VRF. A real VRF/randomness beacon would be a separate protocol change with additional dependencies and analysis. Retaining the old field name `vrf_proof` preserves code familiarity but does not change this limitation.

**Selected-chain balances:** The chosen branch determines spendable coins. A reorganisation can reverse an apparent confirmation and recompute balances. There is no checkpoint-finality protocol or required confirmation depth. Applications must not treat the first appearance as an irreversible real-money settlement.

**Chain weight:** Weight sums each block's complete registered stake snapshot, excluding genesis. It does not sum the producer's self-declared number without validation. The tie-breaker provides convergence for known equal-weight alternatives; it is not a finality proof or a defence against an adversary controlling most stake.

**Slashing scope:** Evidence is for conflicting proposals sharing a parent and slot. The penalty burns up to the offending stake that is still locked, removes the validator from the active registry, and occurs only in a block containing valid evidence. Other transactions from its older block remain part of the ledger. A subsequent funded stake transaction can register again. Evidence must reach an honest validator; detection is not guaranteed across permanently disconnected partitions.

**Empty slots and blocks:** A slot may have zero or multiple winners. Eligible nodes can produce empty blocks so the chain can progress without waiting for an ordinary payment. Rewards remain the original 6 coins per accepted block.

## Networking and signalling changes

| File | Why | How / boundary |
|---|---|---|
| `signalling_server.py` | Remove dependence on a predefined blockchain bootstrap node. | Create/join a named room; sign a fresh registration challenge; return that room's peer directory and genesis identity; notify joins/leaves; reconnect clients after outages. Blockchain traffic remains direct. |
| `consensus/pos/p2p.py` | Keep rooms and protocol versions from accidentally mixing. | Every envelope carries `pos-v2`, room ID and genesis identity. The node pins its network identity before accepting history. Signed peer identity records bind advertised details to a key. |
| `consensus/pos/p2p.py` | Malformed data or concurrent callbacks should not tear down state processing. | Validate message structure, catch and count rejected messages, serialize state mutation with an asyncio lock, and initialise state before loading saved data. Slow validation and child-process execution run off the network event loop. |
| `network_utils.py` | Remembering every message forever wastes memory. | Keep at most 10,000 recent message IDs. Consensus transaction replay protection still uses history, even after an old message ID is evicted. |
| `consensus/pos/p2p.py` | Unlimited reconnect attempts/connections can duplicate work or exhaust resources. | Track in-flight and established outgoing connections, limit counts, impose message/handshake/send bounds, and shut down workers and sockets on exit. |

Rooms allow at most 128 members, and the directory at most 100 remembered rooms. Nodes remember at most 255 other peers, accept up to 32 incoming connections and initiate up to 8 outgoing connections. Pools are capped at 1,000 transactions. Peer frames are limited to 1 MiB and histories to 5,000 blocks; the frame limit can be reached before the block limit. These bounds make a small demonstration more predictable; they do not establish Internet-scale denial-of-service resistance.

The directory is an in-memory service. The original saved creator can re-register the same genesis after a directory restart, and the other nodes reconnect. Existing peer connections continue while it is unavailable. A new participant needs a reachable member to supply history. Room names are not secret access tokens, peer self-registration is not a trusted real-world identity, and learning genesis from the directory is a trust-on-first-use step. There is no NAT traversal, relay, TLS configuration, private-room access control or persistent directory database.

## Web and usability changes

`web_app.py` connects a Flask HTTP server to the node's existing asyncio loop. `web/templates/index.html`, `web/static/style.css` and `web/static/app.js` provide the page. Flask and Werkzeug were already dependencies, so this avoids introducing a separate frontend build system.

The page shows balance, locked stake, block height, chain weight, slot timing, discovered/connected peers, validator stake, recent blocks, pending transactions, rejection events and confirmed penalties. It provides transfer, staking, deploy and invoke forms. Malicious mode adds an overspending button. A small connection diagram shows up to eight peers; the table lists all discovered peers. It shows this node's connections, not a claimed complete global topology. Data refreshes every two seconds, so it is near-real-time polling, not a WebSocket UI stream.

The server listens on loopback only. Commands require a random token delivered in the page, JSON input, and a permitted host/origin. Untrusted names and events are displayed as text. The page receives public keys, never private keys. These controls reduce accidental cross-site commands; they are not a multi-user login system. Other users or processes on the same computer may still be able to control a locally accessible node. Do not expose this management UI as a public service.

`start_peer.py` adds explicit arguments while retaining the old no-argument interactive launcher. Important options are `--room`, `--create-room`, `--signalling`, `--web-port`, `--save`, `--load`, `--observer`, `--malicious`, `--cli` and `--epoch-seconds`. A creator must initially produce blocks. Inputs reject impossible ports and incompatible startup options. Terminal command errors now give a message rather than stopping the node; blocking editor/file/contract work is moved off the event loop where used.

The web dashboard intentionally does not reproduce the legacy IPFS file picker or provide a contract-state inspector. Core PoS, networking, transaction, staking and contract controls are available; optional file transfer remains in the terminal menu.

## Storage, shared helpers and existing integrations

| File | Why changed | How changed / what was retained |
|---|---|---|
| `storage/storage_manager.py` and PoS callers | Nodes in one room should not overwrite each other's wallet/history; partial JSON writes should not destroy the previous checkpoint. | Namespace PoS data by protocol, room hash and peer port. Write a complete temporary file, flush it, and atomically replace the destination. Reads of absent storage no longer create folders. Keep the existing storage function names. |
| `shared_blockchain_structures.py` | Fix incorrect duplicate semantics, a broken `CommonChain` constructor decoration, and support network-bound signatures. | Equality uses transaction ID; history lookup scans the requested prefix and returns the correct answer; normal constructor restored; optional `network` added to transactions. Legacy transactions omit it as before. |
| `consensus/pos/mal_node.py` | A second copy of the entire node duplicated bugs and would drift from the honest implementation. | Replace duplication with a subclass of the honest node. Override publication to deliberately double-sign and add an overspending attack. This accounts for much of the deleted code. |
| `smart_contract/validation.py` | A peer-reported contract result cannot be accepted on trust. | Reconstruct deployed code and preceding state, run each invocation in order, and compare state and charge. |
| `smart_contract/secure_executor.py` | A child process that crashes or times out must not look like successful execution. | Require a complete success result and clean exit; use a monotonic deadline, enforce the existing memory ceiling, and terminate/join the child during cleanup. |
| `smart_contract/smart_contract.py` | Ordinary restricted dictionary access and helper function names failed in valid examples. | Provide the appropriate RestrictedPython guards and a shared execution namespace. Keep the existing gas meter and restricted execution design. |
| `ipfs/ipfs.py` and PoS daemon setup | Multiple local nodes should point IPFS operations at the intended repository. | Pass the node-specific environment to add/download commands, add command timeouts and use a node-specific IPFS directory. The external daemon and legacy terminal workflow are retained. |
| `README.md`, this guide and `docs/QUICK_START.md` | Original installation instructions and PoS descriptions no longer matched runnable behaviour. | Correct the repository directory and Python version, document room commands, restart rules, protocol incompatibility, simplified consensus and test commands. Preserve the project theory and original author credits. |

Old PoS data is left in place. A v2 node uses a new namespace, and explicitly rejects an old-format chain if supplied. Automatic migration would falsely imply old unauthenticated consensus data could be trusted under the new rules. Private keys are still plaintext local files, not an encrypted keystore. Keeping the same room and port is essential when resuming with `--load`; omitting `--load` intentionally requests a new identity.

IPFS needs a separate installed executable. The existing offset-based daemon port scheme can collide with other services or nearby node ports; multi-daemon IPFS operation was not verified. Core room discovery, PoS and the dashboard have no IPFS runtime dependency. The RestrictedPython layer and resource limits are not a guarantee that arbitrary hostile Python is safely contained.

## How the changes were checked

The review traced data from creation through serialization, network reception, block inclusion, fork selection, saving and loading. That matters because a locally correct helper is insufficient when another path bypasses it. Tests exercise those boundaries, including genuine signatures and changed histories, rather than only checking helper return values in isolation.

The final automated suite contains **46 passing tests**: 29 core consensus/shared checks, 10 peer/web checks, and 7 real-WebSocket signalling checks. It was run with Python 3.12.14, ecdsa 0.19.1, websockets 15.0.1 and Flask 3.1.1 using the existing environment's packages. No package installation was needed. The normal project Python executable was denied by the execution environment, so verification used the bundled Python 3.12 interpreter with the project's existing site-packages on its import path.

| Evidence | What it establishes |
|---|---|
| Lottery winner/loser, varied signatures, stake snapshot tampering and seed/slot tests | A fixed key/seed cannot reroll eligibility using new signatures; forged consensus claims are rejected. |
| Duplicate, replay, non-finite, fractional precision, combined overspend and network-binding cases | Invalid accounting inputs and already-used signed requests cannot be accepted through the tested paths. |
| Live/history agreement past five blocks, equal-weight ties and shorter/heavier fork tests | The same validity and selection rules apply to replay and competing histories. |
| Signed conflicting headers, identical/forged evidence and different creators | Genuine double-sign evidence is distinguished from ordinary forks; penalties replay and cannot be included twice. |
| Node storage, protocol serialization, stale contract/reorganisation and contract-result tests | State survives supported serialization, identities are separated, and a stale invocation cannot stall production. |
| Web command, token, cross-origin and malformed-message checks | Tested invalid requests are rejected while subsequent valid requests can still run. |
| Four real Python nodes in two rooms, including a malicious node | Discovery, transfer confirmation, confirmed stakes, slashing, overspending rejection, wallet/history restart, directory outage/recovery, room isolation and honest-tip convergence work together. |
| Contract deployment and invocation in that real network | Both honest nodes see deployment, and all three room nodes verify and save the invocation. |
| Continued dashboard polling and advancing blocks after invocation | Periodic sync does not repeatedly stall the node on an unchanged contract history in the tested run. |
| Browser inspection and a transfer submitted through the actual page | The dashboard renders and its form reaches the live node; no browser console errors were observed during inspection. |

The process integration test uses temporary ports, wallets, logs and data and cleans them up on normal completion. It is reproducible with `python -B -m tests.integration_demo`. It uses three-second slots to keep testing practical. The normal demonstration guide uses ten seconds; the protocol default is sixty.

These checks are evidence of the listed behaviours, not a mathematical proof of consensus security. They do not certify long-running performance, adversarial partitions, public Internet operation, all possible contracts, every Python version, legacy PoW/PoA correctness or external IPFS functionality. Initial download still validates the full history. Later sync reuses the validated common prefix, but accounting and hash comparisons still scan history and become expensive as it grows. A cached ledger state engine, authenticated persistent networking and formal finality would be separate work.

## Questions an evaluator may ask

**“Why did you change more than the obvious PoS comparisons?”**  
Because stake, signatures, balances, replay and fork choice interact. Reversing a comparison still leaves different acceptance paths disagreeing. I kept the existing modules and classes and gave those paths shared rules.

**“What makes this Proof of Stake?”**  
Eligibility depends on confirmed locked stake, and a conflicting signature can cost that stake. More stake gives a larger fraction of eligible hash outputs. Creating another unfunded wallet gives no weight.

**“Is your lottery a VRF?”**  
No. It is a public, verifiable hash lottery. It prevents rerolling by changing a signature for the same key/seed, but it is predictable and is not a standard VRF. I documented that rather than claiming cryptographic properties I did not implement.

**“What prevents the producer inventing its stake?”**  
The receiver reconstructs the registry from earlier accepted blocks. Both the producer's stake and the full snapshot must match it exactly. Announcing a number is insufficient.

**“Why put staking on the chain?”**  
Otherwise two nodes might hear different announcements and calculate different odds. A confirmed stake transaction gives every node the same activation point, including a late joiner replaying history.

**“Can someone send their staked coins away and avoid punishment?”**  
Ordinary transfers can use only balance minus locked stake. This simplified version does not allow stake decreases, so collateral remains available. A full withdrawal system would need an explicit unbonding/evidence period.

**“Why use integer coin units?”**  
Floating-point numbers can round differently than the decimal amounts a user intended. A coin has one million units here, making addition, subtraction and spending checks exact. The interface converts back to coin amounts.

**“How do you know a double-sign report is not a lie?”**  
It must include two verifiable signatures by the same eligible key over different block hashes for the same parent and slot. Repeated copies of one block, or proposals from different validators, do not qualify.

**“Why not delete the malicious validator's old block?”**  
Deleting earlier effects can invalidate transactions other people already made from that history. A later, verifiable penalty is deterministic and can be reconstructed by a new node.

**“Does your deterministic tie-break guarantee finality?”**  
No. It ensures nodes with the same valid candidates choose the same winner. New forks can still change the selected chain. This project does not implement checkpoint finality.

**“Does the signalling server centralise the blockchain?”**  
Discovery depends on a directory, but validation does not. The server introduces peers; nodes exchange and validate blocks directly. The integration test stops discovery and shows existing peers continuing to produce blocks. First-time discovery still trusts the directory's introduction and network identity.

**“Are rooms private? Will this work through any router?”**  
No. A room is a discovery namespace, not authentication. Nodes need reachable advertised addresses. This implementation targets the requested same-machine setup and can be configured for a trusted LAN; it does not implement NAT traversal.

**“Why Flask and plain JavaScript?”**  
Flask was already installed in the original requirements. One HTML template and static files expose the existing node without adding a build tool, framework or independent consensus implementation.

**“Why did you remove so much from the malicious-node file?”**  
Most of it duplicated the honest node. Inheriting the common behaviour means the attack tests the same network and serialization paths. Only deliberate misbehaviour needs to differ.

**“Can a web page steal the private key?”**  
The API never returns it. The node signs locally. However, this is a local management page, not a hardened multi-user wallet, and saved keys remain plaintext on disk.

**“What is the biggest remaining limitation?”**  
The public lottery and simple heaviest-chain model are educational. They do not provide production randomness, irreversible finality or security against majority-stake and grinding attacks. I can defend the tested bug fixes without claiming the simulator is a production blockchain.

**“What did you leave alone to keep scope controlled?”**  
The original project layout, wallet and basic data classes, terminal launcher, dependencies and legacy PoW/PoA engines. I added required features alongside the existing PoS path and only changed shared helpers or integrations where that path needed them.

**“How can we verify your claims?”**  
Run the 46 tests, then the four-node integration scenario, and use the beginner walkthrough to observe a transfer, stake activation and a malicious penalty. Read `valid_block`, `valid_transaction`, `registered_stakes`, `chain_rank` and `valid_evidence` to see the protocol rules directly.

## Suggested order for learning the code

1. Run the two-node walkthrough and identify pending versus confirmed transactions.
2. Read `Transaction` in `shared_blockchain_structures.py` and follow a signature into `valid_transaction`.
3. Read `ledger_state` and `registered_stakes`: this is how a history becomes balances and voting weight.
4. Read `epoch_seed`, `lottery_wins` and `valid_block`: this is how a proposal earns acceptance.
5. Read `chain_rank`, `Chain.rewrite` and `Peer.refresh_state`: this is how forks change local state.
6. Read the small malicious subclass and `valid_evidence`, then reproduce the penalty demonstration.
7. Finally read the signalling server and web adapter. They introduce and control nodes; they do not replace the consensus rules.

You do not need to memorise the entire implementation. Be able to explain one transaction's journey, where stake comes from, what every node verifies, how a fork is resolved, and what this simulator does not promise.
