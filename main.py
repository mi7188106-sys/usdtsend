import json
from decimal import Decimal
from web3 import Web3
from web3.exceptions import TransactionNotFound, TimeExhausted

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import traceback

app = FastAPI()

CHAIN_ID = 56
GAS_LIMIT_FALLBACK = 100000

# ------------------------------------------------------------
#  USDT ABI
# ------------------------------------------------------------
USDT_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    }
]

# ------------------------------------------------------------
# Utils
# ------------------------------------------------------------
def format_decimal(value, precision=18):
    try:
        d = Decimal(str(value))
        s = f"{d:.{precision}f}"
        if '.' in s:
            s = s.rstrip('0').rstrip('.')
        return s if s else "0"
    except:
        return str(value)

def error_response(code, message, details=None, status_code=400):
    payload = {
        "success": False,
        "error": {
            "code": code,
            "message": message
        }
    }
    if details:
        payload["error"]["details"] = details

    return JSONResponse(content=payload, status_code=status_code)

# ------------------------------------------------------------
# GLOBAL ERROR
# ------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return error_response(
        "UNEXPECTED_ERROR",
        "Something went wrong",
        {
            "error": str(exc),
            "trace": traceback.format_exc()
        },
        status_code=500
    )

# ------------------------------------------------------------
# HEALTH CHECK
# ------------------------------------------------------------
@app.get("/")
async def root():
    return {"success": True, "message": "API working fine 🚀"}

# ------------------------------------------------------------
# CHECK BALANCE
# ------------------------------------------------------------
@app.post("/check-balance")
async def check_balance(request: Request):
    try:
        data = await request.json()

        PRIVATE_KEY = data.get("private_key")
        USDT_ADDRESS = data.get("usdt_address", "0x55d398326f99059fF775485246999027B3197955")
        BSC_RPC = data.get("rpc", "https://bsc-dataseed.binance.org/")

        if not PRIVATE_KEY:
            return error_response("MISSING_KEY", "Private key required")

        w3 = Web3(Web3.HTTPProvider(BSC_RPC))
        if not w3.is_connected():
            return error_response("CONNECTION_ERROR", "RPC failed")

        account = w3.eth.account.from_key(PRIVATE_KEY)
        wallet = account.address

        usdt = w3.eth.contract(address=w3.to_checksum_address(USDT_ADDRESS), abi=USDT_ABI)

        decimals = usdt.functions.decimals().call()

        usdt_balance = usdt.functions.balanceOf(wallet).call()
        bnb_balance = w3.eth.get_balance(wallet)

        return {
            "success": True,
            "wallet": wallet,
            "balances": {
                "usdt": format_decimal(usdt_balance / (10 ** decimals), decimals),
                "bnb": format_decimal(w3.from_wei(bnb_balance, 'ether'), 18)
            }
        }

    except Exception as e:
        return error_response("BALANCE_ERROR", str(e))

# ------------------------------------------------------------
# SEND USDT
# ------------------------------------------------------------
@app.post("/send-usdt")
async def send_usdt(request: Request):

    try:
        headers = request.headers

        BSC_RPC = headers.get("BSC_RPC", "https://bsc-dataseed.binance.org/")
        USDT_ADDRESS = headers.get("USDT_ADDRESS")
        SENDER = headers.get("SENDER")
        PRIVATE_KEY = headers.get("PRIVATE_KEY")
        RECEIVER = headers.get("RECEIVER")
        AMOUNT_USDT = headers.get("AMOUNT_USDT")

        if not all([USDT_ADDRESS, SENDER, PRIVATE_KEY, RECEIVER, AMOUNT_USDT]):
            return error_response("MISSING_HEADERS", "Required headers missing")

        AMOUNT_USDT = float(AMOUNT_USDT)

        w3 = Web3(Web3.HTTPProvider(BSC_RPC))
        if not w3.is_connected():
            return error_response("CONNECTION_ERROR", "RPC failed")

        sender_checksum = w3.to_checksum_address(SENDER)
        receiver_checksum = w3.to_checksum_address(RECEIVER)
        usdt_checksum = w3.to_checksum_address(USDT_ADDRESS)

        account = w3.eth.account.from_key(PRIVATE_KEY)
        if account.address.lower() != sender_checksum.lower():
            return error_response("PRIVATE_KEY_MISMATCH", "Mismatch sender")

        usdt = w3.eth.contract(address=usdt_checksum, abi=USDT_ABI)

        decimals = usdt.functions.decimals().call()
        amount_wei = int(AMOUNT_USDT * (10 ** decimals))

        before_usdt = usdt.functions.balanceOf(sender_checksum).call()
        before_bnb = w3.eth.get_balance(sender_checksum)

        if before_usdt < amount_wei:
            return error_response("INSUFFICIENT_USDT", "Low balance")

        nonce = w3.eth.get_transaction_count(sender_checksum)
        gas_price = w3.eth.gas_price

        try:
            gas_limit = int(
                usdt.functions.transfer(receiver_checksum, amount_wei)
                .estimate_gas({'from': sender_checksum}) * 1.2
            )
        except:
            gas_limit = GAS_LIMIT_FALLBACK

        if before_bnb < gas_limit * gas_price:
            return error_response("INSUFFICIENT_BNB", "Not enough gas")

        tx = usdt.functions.transfer(receiver_checksum, amount_wei).build_transaction({
            'chainId': CHAIN_ID,
            'gas': gas_limit,
            'gasPrice': gas_price,
            'nonce': nonce,
        })

        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt.status == 0:
            return error_response("FAILED", "Transaction reverted")

        return {
            "success": True,
            "tx_hash": w3.to_hex(tx_hash),
            "block": receipt.blockNumber
        }

    except TimeExhausted:
        return error_response("TIMEOUT", "Transaction timeout")

    except TransactionNotFound:
        return error_response("NOT_FOUND", "Transaction not found")

    except Exception as e:
        return error_response(
            "CRITICAL_ERROR",
            str(e),
            {"trace": traceback.format_exc()},
            500
        )        return {k: to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_serializable(v) for v in obj]
    return obj

def error_response(code, message, details=None, status_code=400):
    payload = {
        "success": False,
        "error": {
            "code": code,
            "message": message
        }
    }
    if details:
        payload["error"]["details"] = details

    return JSONResponse(content=to_serializable(payload), status_code=status_code)

# ------------------------------------------------------------
#  Global Exception Handlers
# ------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return error_response(
        "UNEXPECTED_ERROR",
        "Something went wrong",
        {
            "error": str(exc),
            "trace": traceback.format_exc()
        },
        status_code=500
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return error_response(
        "VALIDATION_ERROR",
        "Request validation failed",
        {"details": exc.errors()},
        status_code=422
    )

# ------------------------------------------------------------
#  Main Endpoint
# ------------------------------------------------------------
@app.post("/send-usdt")
async def send_usdt(request: Request):

    try:
        headers = request.headers

        BSC_RPC = headers.get("BSC_RPC", "https://bsc-dataseed.binance.org/")
        USDT_ADDRESS = headers.get("USDT_ADDRESS")
        SENDER = headers.get("SENDER")
        PRIVATE_KEY = headers.get("PRIVATE_KEY")
        RECEIVER = headers.get("RECEIVER")
        AMOUNT_USDT = headers.get("AMOUNT_USDT")

        if not all([USDT_ADDRESS, SENDER, PRIVATE_KEY, RECEIVER, AMOUNT_USDT]):
            return error_response("MISSING_HEADERS", "Required headers missing")

        try:
            AMOUNT_USDT = float(AMOUNT_USDT)
            if AMOUNT_USDT <= 0:
                return error_response("INVALID_AMOUNT", "Amount must be > 0")
        except:
            return error_response("INVALID_AMOUNT", "Invalid number")

        w3 = Web3(Web3.HTTPProvider(BSC_RPC))
        if not w3.is_connected():
            return error_response("CONNECTION_ERROR", "RPC connection failed")

        try:
            sender_checksum = w3.to_checksum_address(SENDER)
            receiver_checksum = w3.to_checksum_address(RECEIVER)
            usdt_checksum = w3.to_checksum_address(USDT_ADDRESS)
        except Exception as e:
            return error_response("INVALID_ADDRESS", str(e))

        try:
            account = w3.eth.account.from_key(PRIVATE_KEY)
            if account.address.lower() != sender_checksum.lower():
                return error_response("PRIVATE_KEY_MISMATCH", "Mismatch sender")
        except Exception as e:
            return error_response("INVALID_PRIVATE_KEY", str(e))

        usdt = w3.eth.contract(address=usdt_checksum, abi=USDT_ABI)

        try:
            decimals = usdt.functions.decimals().call()
            amount_wei = int(AMOUNT_USDT * (10 ** decimals))
        except Exception as e:
            return error_response("DECIMAL_ERROR", str(e))

        try:
            before_usdt = usdt.functions.balanceOf(sender_checksum).call()
            before_bnb = w3.eth.get_balance(sender_checksum)
        except Exception as e:
            return error_response("BALANCE_ERROR", str(e))

        if before_usdt < amount_wei:
            return error_response("INSUFFICIENT_USDT", "Low USDT balance")

        nonce = w3.eth.get_transaction_count(sender_checksum)
        gas_price = w3.eth.gas_price

        try:
            gas_limit = int(
                usdt.functions.transfer(receiver_checksum, amount_wei)
                .estimate_gas({'from': sender_checksum}) * 1.2
            )
        except:
            gas_limit = GAS_LIMIT_FALLBACK

        if before_bnb < gas_limit * gas_price:
            return error_response("INSUFFICIENT_BNB", "Not enough gas")

        tx = usdt.functions.transfer(receiver_checksum, amount_wei).build_transaction({
            'chainId': CHAIN_ID,
            'gas': gas_limit,
            'gasPrice': gas_price,
            'nonce': nonce,
        })

        try:
            signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        except Exception as e:
            return error_response("SEND_FAILED", str(e))

        try:
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        except TimeExhausted:
            return error_response("TIMEOUT", "Transaction timeout")
        except TransactionNotFound:
            return error_response("NOT_FOUND", "Transaction not found")

        if receipt.status == 0:
            return error_response("FAILED", "Transaction reverted")

        return JSONResponse(content={
            "success": True,
            "tx_hash": w3.to_hex(tx_hash),
            "block": receipt.blockNumber
        })

    except Exception as e:
        # fallback (extra safety)
        return error_response(
            "CRITICAL_ERROR",
            "Unhandled exception",
            {
                "error": str(e),
                "trace": traceback.format_exc()
            },
            status_code=500
        )
