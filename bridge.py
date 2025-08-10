from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account
import json, sys
from web3.exceptions import Web3RPCError

def _get_recent_logs_per_block(event, w3, last_n=5):
    latest = w3.eth.block_number
    logs = []
    start = max(0, latest - (last_n - 1))
    for bn in range(start, latest + 1):
        try:
            logs.extend(event.get_logs(from_block=bn, to_block=bn))
        except Web3RPCError as e:
            if "limit exceeded" in str(e):
                blk = w3.eth.get_block(bn)
                try:
                    logs.extend(event.get_logs(block_hash=blk.hash))
                except Exception:
                    pass
            else:
                raise
    return logs

AVAX_RPC = "https://api.avax-test.network/ext/bc/C/rpc"
BSC_RPC  = "https://data-seed-prebsc-1-s1.binance.org:8545/"

def connect_to(which: str) -> Web3:
    url = AVAX_RPC if which == "source" else BSC_RPC
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    assert w3.is_connected(), f"Failed to connect to {which}"
    return w3

def _load_contract(w3: Web3, info: dict):
    return w3.eth.contract(address=Web3.to_checksum_address(info["address"]), abi=info["abi"])

def _read_pk(filename="secret_key.txt") -> str:
    with open(filename, "r") as f:
        pk = f.readline().strip()
    return pk if pk.startswith("0x") else "0x"+pk

def _is_eip1559(w3: Web3) -> bool:
    return w3.eth.get_block("latest").get("baseFeePerGas") is not None

def _sign_and_send(w3: Web3, tx: dict, pk: str):
    acct = Account.from_key(pk)
    tx.setdefault("nonce", w3.eth.get_transaction_count(acct.address))
    tx.setdefault("chainId", w3.eth.chain_id)
    if _is_eip1559(w3):
        base = w3.eth.gas_price
        tx.setdefault("maxFeePerGas", base * 2)
        tx.setdefault("maxPriorityFeePerGas", int(base * 0.1) or 1_000_000_000)
        tx.pop("gasPrice", None)
        tx.setdefault("type", 2)
    else:
        tx.setdefault("gasPrice", w3.eth.gas_price)
        tx.pop("maxFeePerGas", None); tx.pop("maxPriorityFeePerGas", None)
        tx.setdefault("type", 0)
    try:
        tx.setdefault("gas", int(w3.eth.estimate_gas(tx) * 1.2))
    except Exception:
        tx.setdefault("gas", 300000)

    signed = w3.eth.account.sign_transaction(tx, pk)
    raw = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction", None)
    h = w3.eth.send_raw_transaction(raw)

    print("tx:", h.hex())
    w3.eth.wait_for_transaction_receipt(h, timeout=45)
    return h

def get_contract_info(chain: str, path="contract_info.json"):
    with open(path, "r") as f:
        return json.load(f)[chain]

def scan_blocks(chain: str, contract_info="contract_info.json"):
    if chain not in ("source","destination"):
        print("Invalid chain:", chain); return 0

    pk = _read_pk("secret_key.txt")
    src_w3, dst_w3 = connect_to("source"), connect_to("destination")
    src = _load_contract(src_w3, get_contract_info("source", contract_info))
    dst = _load_contract(dst_w3, get_contract_info("destination", contract_info))
    from_addr = Account.from_key(pk).address

    if chain == "source":
        start = max(0, src_w3.eth.block_number - 5)
        logs = _get_recent_logs_per_block(src.events.Deposit(), src_w3, last_n=5)
        if not logs:
            print("No Deposit events found."); return 0
        for e in logs:
            token, recipient, amount = e.args["token"], e.args["recipient"], int(e.args["amount"])
            tx = dst.functions.wrap(token, recipient, amount).build_transaction({"from": from_addr})
            _sign_and_send(dst_w3, tx, pk)
        return len(logs)

    if chain == "destination":
        start = max(0, dst_w3.eth.block_number - 5)
        logs = _get_recent_logs_per_block(dst.events.Unwrap(), dst_w3, last_n=5)
        if not logs:
            print("No Unwrap events found."); return 0
        for e in logs:
            underlying, to_addr, amount = e.args["underlying_token"], e.args["to"], int(e.args["amount"])
            tx = src.functions.withdraw(underlying, to_addr, amount).build_transaction({"from": from_addr})
            _sign_and_send(src_w3, tx, pk)
        return len(logs)

if __name__ == "__main__":
    if len(sys.argv) == 2:
        scan_blocks(sys.argv[1])
    else:
        scan_blocks("source")
        scan_blocks("destination")
