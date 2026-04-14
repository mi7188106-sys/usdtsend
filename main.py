import json
import asyncio
from decimal import Decimal
from functools import lru_cache
from time import time
from web3 import Web3
from web3.exceptions import TransactionNotFound, TimeExhausted
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
import traceback
import httpx

app = FastAPI()

CHAIN_ID = 56
GAS_LIMIT_FALLBACK = 100000
PRICE_CACHE_TTL = 30  # seconds

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
#  Price helper
# ------------------------------------------------------------
_price_cache = {"price": None, "timestamp": 0}

async def get_bnb_usdt_price() -> float | None:
    """Fetch BNB/USDT price from Binance, with simple TTL cache."""
    global _price_cache
    now = time()
    if _price_cache["price"] is not None and (now - _price_cache["timestamp"]) < PRICE_CACHE_TTL:
        return _price_cache["price"]

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("https://api.binance.com/api/v3/ticker/price?symbol=BNBUSDT")
            if resp.status_code == 200:
                data = resp.json()
                price = float(data["price"])
                _price_cache = {"price": price, "timestamp": now}
                return price
    except Exception:
        pass
    return None

# ------------------------------------------------------------
#  Utils
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

def to_serializable(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: to_serializable(v) for k, v in obj.items()}
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
        {"error": str(exc), "trace": traceback.format_exc()},
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
#  Health Check
# ------------------------------------------------------------
@app.get("/")
async def health_check():
    return JSONResponse(content={"status": "ok", "service": "usdt-sender"})

# ------------------------------------------------------------
#  Balance Check Endpoint (enhanced)
# ------------------------------------------------------------
@app.post("/balance")
async def check_balance(request: Request):
    """
    Check BNB and USDT balance for a given private key.
    JSON body: {"private_key": "0x...", "usdt_address": "0x...", "bsc_rpc": "https://..."}
    Returns balances and their USD values (BNB priced in USDT, USDT ~ USD).
    """
    try:
        body = await request.json()
    except Exception:
        return error_response("INVALID_JSON", "Request body must be valid JSON")

    private_key = body.get("private_key")
    if not private_key:
        return error_response("MISSING_PRIVATE_KEY", "private_key is required")

    usdt_address = body.get("usdt_address") or request.headers.get("USDT_ADDRESS")
    bsc_rpc = body.get("bsc_rpc") or request.headers.get("BSC_RPC", "https://bsc-dataseed.binance.org/")

    if not usdt_address:
        return error_response("MISSING_USDT_ADDRESS", "USDT_ADDRESS not provided")

    w3 = Web3(Web3.HTTPProvider(bsc_rpc))
    if not w3.is_connected():
        return error_response("CONNECTION_ERROR", "RPC connection failed")

    try:
        account = w3.eth.account.from_key(private_key)
        address = account.address
        checksum_address = w3.to_checksum_address(address)
    except Exception as e:
        return error_response("INVALID_PRIVATE_KEY", str(e))

    # BNB balance
    bnb_wei = w3.eth.get_balance(checksum_address)
    bnb_balance = w3.from_wei(bnb_wei, 'ether')
    bnb_balance_str = format_decimal(bnb_balance, 18)

    # USDT balance
    try:
        usdt_checksum = w3.to_checksum_address(usdt_address)
        usdt_contract = w3.eth.contract(address=usdt_checksum, abi=USDT_ABI)
        decimals = usdt_contract.functions.decimals().call()
        usdt_wei = usdt_contract.functions.balanceOf(checksum_address).call()
        usdt_balance = usdt_wei / (10 ** decimals)
        usdt_balance_str = format_decimal(usdt_balance, decimals)
    except Exception as e:
        return error_response("USDT_BALANCE_ERROR", f"Failed to fetch USDT balance: {str(e)}")

    # Get BNB price in USDT
    bnb_price = await get_bnb_usdt_price()
    bnb_usdt_value = None
    if bnb_price is not None:
        bnb_usdt_value = format_decimal(bnb_balance * bnb_price, 2)
    # USDT value in USD (assuming 1:1)
    usdt_usd_value = format_decimal(usdt_balance, 2)

    return JSONResponse(content={
        "success": True,
        "address": checksum_address,
        "bnb": {
            "wei": str(bnb_wei),
            "formatted": bnb_balance_str,
            "usd_value": bnb_usdt_value  # in USDT (≈USD)
        },
        "usdt": {
            "wei": str(usdt_wei),
            "formatted": usdt_balance_str,
            "usd_value": usdt_usd_value,
            "decimals": decimals,
            "contract": usdt_checksum
        },
        "price": {
            "bnb_usdt": bnb_price
        }
    })

# ------------------------------------------------------------
#  Send USDT Endpoint (enhanced)
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

        # Fetch before balances
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

        # After balances (fetch again after transaction)
        try:
            after_usdt = usdt.functions.balanceOf(sender_checksum).call()
            after_bnb = w3.eth.get_balance(sender_checksum)
        except Exception as e:
            # Fallback: compute expected values if fetch fails
            after_usdt = before_usdt - amount_wei
            after_bnb = before_bnb - (receipt.gasUsed * gas_price)

        # Gas details
        gas_used = receipt.gasUsed
        gas_cost_wei = gas_used * gas_price
        gas_cost_bnb = w3.from_wei(gas_cost_wei, 'ether')
        gas_cost_bnb_str = format_decimal(gas_cost_bnb, 18)

        # BNB price for USDT conversion
        bnb_price = await get_bnb_usdt_price()
        gas_cost_usdt = None
        if bnb_price is not None:
            gas_cost_usdt = format_decimal(gas_cost_bnb * bnb_price, 6)

        # Format balances
        before_usdt_formatted = format_decimal(before_usdt / (10 ** decimals), decimals)
        after_usdt_formatted = format_decimal(after_usdt / (10 ** decimals), decimals)
        before_bnb_formatted = format_decimal(w3.from_wei(before_bnb, 'ether'), 18)
        after_bnb_formatted = format_decimal(w3.from_wei(after_bnb, 'ether'), 18)

        response_data = {
            "success": True,
            "amount": format_decimal(AMOUNT_USDT, decimals),
            "currency": "USDT",
            "token_contract": usdt_checksum,
            "chain_id": CHAIN_ID,
            "before_transaction_balance": {
                "usdt": before_usdt_formatted,
                "bnb": before_bnb_formatted
            },
            "after_transaction_balance": {
                "usdt": after_usdt_formatted,
                "bnb": after_bnb_formatted
            },
            "gas": {
                "gas_used": gas_used,
                "gas_price_wei": gas_price,
                "gas_cost_wei": gas_cost_wei,
                "gas_cost_bnb": gas_cost_bnb_str,
                "gas_cost_usdt": gas_cost_usdt,  # optional, if price available
                "gas_currency": "BNB"
            },
            "transaction_hash": w3.to_hex(tx_hash),
            "block_number": receipt.blockNumber,
            "receiver": receiver_checksum,
            "sender": sender_checksum
        }

        # Include price info if available
        if bnb_price is not None:
            response_data["price"] = {
                "bnb_usdt": bnb_price
            }

        return JSONResponse(content=to_serializable(response_data))

    except Exception as e:
        return error_response(
            "CRITICAL_ERROR",
            "Unhandled exception",
            {"error": str(e), "trace": traceback.format_exc()},
            status_code=500
        )
