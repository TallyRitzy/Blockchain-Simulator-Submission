# Beginner walkthrough

These commands run the revised project at `J:\Tathwa\Blockchain-Simulation`. They create local simulation coins, not real money. Use separate PowerShell terminals for separate processes, and leave each running.

## 1. Check Python and dependencies

In a terminal:

```powershell
cd J:\Tathwa\Blockchain-Simulation
.\venv\Scripts\python.exe --version
```

Use Python 3.12 or newer. The implementation was checked with Python 3.12.14. If the existing environment already works, keep it. If you need to create one, first install Python 3.12 from the official Python installer, then run:

```powershell
py -3.12 -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

Using the full executable path avoids PowerShell activation-policy problems. On Linux/macOS, use `venv/bin/python` instead. No new Python dependencies were added for the dashboard or signalling service; Flask and websockets were already in `requirements.txt`.

## 2. Start the directory

Terminal 1:

```powershell
cd J:\Tathwa\Blockchain-Simulation
.\venv\Scripts\python.exe signalling_server.py
```

This listens on port 8765. Think of it as a room receptionist: it introduces nodes to each other. It does not approve transactions or choose blocks.

## 3. Create the room

Terminal 2:

```powershell
cd J:\Tathwa\Blockchain-Simulation
.\venv\Scripts\python.exe start_peer.py --port 5000 --name Alice --room classroom --create-room --web-port 8000 --epoch-seconds 10 --save
```

Open **http://127.0.0.1:8000**. Alice creates the first block, called genesis. She initially has 56 total coins and 10 locked as stake, leaving 46 available. Rewards can increase this before you open the page.

`5000` is Alice's peer port; `8000` is her browser port. `classroom` is the room ID. Only the first node uses `--create-room`. `--epoch-seconds 10` makes the demonstration faster; the normal default is 60. New members read the duration from genesis automatically.

## 4. Join the room

Terminal 3:

```powershell
cd J:\Tathwa\Blockchain-Simulation
.\venv\Scripts\python.exe start_peer.py --port 5001 --name Bob --room classroom --web-port 8001 --save
```

Open **http://127.0.0.1:8001**. Wait until both pages say “Node online” and show the other node as connected. Bob starts with no coins and no active stake. Having a wallet alone does not give him voting weight.

## 5. Transfer and stake

1. On Alice's page, choose Bob, enter `15`, and select **Send transaction**.
2. Wait for Bob's total balance to reach 15. A submission message means pending, not confirmed. A block must include it.
3. On Bob's page, enter `5` under **New total stake in whole coins**, then select **Register stake**.
4. Wait until Bob shows an active stake of 5. His available balance is then 10, unless rewards or other transactions have changed it.
5. Watch block cards and consensus events on both pages. Bob can now win slots too.

The stake box sets a **new total**, not an additional deposit: entering 7 after staking 5 locks 2 more. This demonstration deliberately permits increases only. Lottery rounds can be empty or have competing winners; allow several rounds before assuming something is broken.

## 6. Demonstrate a malicious node

Terminal 4:

```powershell
cd J:\Tathwa\Blockchain-Simulation
.\venv\Scripts\python.exe start_peer.py --port 5002 --name Mallory --room classroom --web-port 8002 --malicious --save
```

Open **http://127.0.0.1:8002**. Send Mallory 15 coins from Alice. After confirmation, register 5 stake on Mallory's page. Keep Alice and Bob online.

When Mallory wins, she deliberately signs two different blocks for the same parent and slot. Honest nodes collect both signatures as evidence. A later block confirms the penalty: Mallory's stake becomes zero and the confirmed-penalties count rises. Her total balance need not decrease by exactly 5 between two screenshots because she might also receive a block reward. The ledger deducts the 5-coin penalty independently of rewards.

Choose a receiver on Mallory's page, then press **Send invalid transaction**. This deliberately broadcasts an amount greater than her balance. An honest node's rejected-message count should increase, and the recipient should not receive those invented coins.

## 7. Optional contracts

Expand **Smart contracts** on Alice's page. Deploy the supplied counter example. Wait until the contract appears in the confirmed-contract dropdown, then select it and invoke `increment` with `[1]`. The fee is deducted only when a valid transaction is confirmed. Nodes re-run the function and compare its reported state and charge. The current dashboard exposes deploy/invoke controls and contract IDs; it does not yet provide a dedicated contract-state inspector.

## 8. Stop, restart and retain your wallet

Use **Ctrl+C** in the node terminals. Stop the signalling terminal last. `--save` keeps data under `storage/pos/v2/<room-hash>/<port>/`.

Restart the directory, then repeat Alice's command with **`--load` added**. Repeat Bob's and Mallory's commands with `--load` too. Keep each node's room and peer port unchanged. Keep `--save` if you want subsequent progress written to disk.

Do not omit `--load` when resuming a saved node: omission requests a fresh wallet. Do not share the storage directory; it contains private keys in plaintext. Old PoS v1 data is not automatically migrated. For an entirely new demonstration, choose a new room ID.

## 9. Reproduce the verification

```powershell
.\venv\Scripts\python.exe -B -m unittest discover -s tests -v
.\venv\Scripts\python.exe -B -m tests.integration_demo
```

The first command runs 46 automated checks. The second runs a real four-node, two-room scenario using temporary ports and temporary storage. It tests transfers, staking, malicious behaviour, restart, directory recovery, isolation and contracts. Normally it exits and cleans up. `--keep-running` is available only if you deliberately want to inspect its temporary web pages; stop it with Ctrl+C when done.

## Common problems

| What you see | What it means / what to do |
|---|---|
| Connection refused on 8765 | Start the signalling server first and leave its terminal running. |
| Room does not exist | Start Alice with `--create-room`; spell the same room ID on all nodes. IDs are case-sensitive. |
| Address already in use | A process already owns that port. Stop your previous node or choose unused peer and web ports. |
| Different network / duplicate wallet or address | Do not run two copies of one saved node. Resume with the right `--load`, room and port, or use a fresh room. |
| Synchronising | Wait for a valid copy of history from an online room member. A brand-new joiner cannot recreate lost history. |
| Stake rejected | Wait for funding, use a positive whole number, and do not decrease the confirmed total. |
| No new block for a while | Keep a funded honest validator online. Multiple validators do not guarantee a winner in every slot. |
| Two peers but four connections | Each pair can have an incoming and an outgoing connection; these are different counts. |
| SyntaxError in old f-strings | Check that the executable is Python 3.12+, not an older global Python. |
| IPFS command missing | IPFS needs its separate executable. It is optional and unnecessary for this walkthrough. |

For LAN use, nodes must advertise addresses reachable by other nodes and share a reachable signalling endpoint. Room discovery does not provide NAT traversal, encryption or private-room authentication. Keep the default local demonstration for the evaluation unless you deliberately configure a trusted LAN.

Read `docs/CHANGES_AND_DEFENCE.md` next. It explains the code, reasons, tests and answers you should understand before presenting.
