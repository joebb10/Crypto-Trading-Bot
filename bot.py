import os
import json
import time
import sqlite3
import numpy as np
import pandas as pd
from web3 import Web3
from dotenv import load_dotenv
from sklearn.linear_model import LinearRegression

load_dotenv()

UNISWAP_V2_ROUTER_ADDRESS = Web3.to_checksum_address('0xedf6066a2b290C185783862C7F4776A2C8077AD1') 
UNI_ADDRESS = Web3.to_checksum_address('0xb33eaad8d922b1083446dc23f610c2567fb5180f') #Uniswap token address - feel free to change the address to any token you'd like.
USDC_ADDRESS = Web3.to_checksum_address('0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359') #USDC address - feel free to change the address to any token you'd like.
AMOUNT_TO_TRADE_USDC = 100
AMOUNT_TO_TRADE_UNI = 5
AMOUNT_TO_CHECK_PRICE = Web3.to_wei(0.00001, 'ether')
PRICE_HISTORY_LENGTH = 100
THRESHOLD_VARIANCE = 3
TRANSACTION_COST = Web3.to_wei(0.00001, 'ether')
GAS_PRICE_INCREMENT = Web3.to_wei(5, 'gwei')
LINEAR_REGRESSION_THRESHOLD = 0.01

with open('uniswap_v2_router_abi.json') as f:
    router_abi = json.load(f)

with open('erc20_abi.json') as f:
    erc20_abi = json.load(f)

alchemy_url = 'https://polygon-mainnet.g.alchemy.com/v2/<your-alchemy-key>' # If you would like to run this bot in any other evm-compatible chain, just change the URL on this line. You can also use Infura or other sources to interact with the chain you would like to.
web3 = Web3(Web3.HTTPProvider(alchemy_url))

account = web3.eth.account.from_key(os.getenv('PRIVATE_KEY'))
web3.eth.default_account = account.address

uniswap_router = web3.eth.contract(address=UNISWAP_V2_ROUTER_ADDRESS, abi=router_abi)
uni = web3.eth.contract(address=UNI_ADDRESS, abi=erc20_abi)
usdc = web3.eth.contract(address=USDC_ADDRESS, abi=erc20_abi)

conn = sqlite3.connect('trading_bot.db')
c = conn.cursor()

c.execute('''CREATE TABLE IF NOT EXISTS price_history (
                timestamp INTEGER,
                amount_usdc_received REAL,
                rsi REAL,
                macd REAL,
                signal_line REAL,
                price_change REAL)''')

def check_and_add_column(cursor, table_name, column_name, column_type):
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [info[1] for info in cursor.fetchall()]
    if column_name not in columns:
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")

check_and_add_column(c, 'price_history', 'rsi', 'REAL')
check_and_add_column(c, 'price_history', 'macd', 'REAL')
check_and_add_column(c, 'price_history', 'signal_line', 'REAL')
check_and_add_column(c, 'price_history', 'price_change', 'REAL')

c.execute('''CREATE TABLE IF NOT EXISTS transactions (timestamp INTEGER, tx_hash TEXT, amount_usdc REAL, direction TEXT)''')
conn.commit()

def approve_token(token_contract, amount):
    try:
        nonce = web3.eth.get_transaction_count(web3.eth.default_account)
        tx = token_contract.functions.approve(UNISWAP_V2_ROUTER_ADDRESS, amount).build_transaction({
            'from': web3.eth.default_account,
            'nonce': nonce,
            'gas': 100000,
            'gasPrice': web3.eth.gas_price + GAS_PRICE_INCREMENT
        })
        signed_tx = web3.eth.account.sign_transaction(tx, private_key=os.getenv('PRIVATE_KEY'))
        tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
        web3.eth.wait_for_transaction_receipt(tx_hash)
        return tx_hash
    except Exception as e:
        print(f'Error approving token: {e}')
        return None

def check_allowance(token_contract, owner, spender):
    return token_contract.functions.allowance(owner, spender).call()

def check_balance(token_contract, owner):
    return token_contract.functions.balanceOf(owner).call()

def swap_uni_to_usdc(amount_to_swap):
    try:
        allowance = check_allowance(uni, web3.eth.default_account, UNISWAP_V2_ROUTER_ADDRESS)
        balance = check_balance(uni, web3.eth.default_account)
        if allowance < amount_to_swap:
            approve_tx_hash = approve_token(uni, amount_to_swap)
            if approve_tx_hash is None:
                print('UNI approval failed.')
                return None, 0
        if balance < amount_to_swap:
            print(f'Insufficient UNI balance. Current balance: {balance}, required: {amount_to_swap}')
            return None, 0

        deadline = int(time.time()) + 300
        nonce = web3.eth.get_transaction_count(web3.eth.default_account)
        amount_out_min = 0

        tx = uniswap_router.functions.swapExactTokensForTokens(
            amount_to_swap,
            amount_out_min,
            [UNI_ADDRESS, USDC_ADDRESS],
            web3.eth.default_account,
            deadline
        ).build_transaction({
            'from': web3.eth.default_account,
            'nonce': nonce,
            'gas': 300000,
            'gasPrice': web3.eth.gas_price + GAS_PRICE_INCREMENT
        })
        signed_tx = web3.eth.account.sign_transaction(tx, private_key=os.getenv('PRIVATE_KEY'))
        tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
        tx_receipt = web3.eth.wait_for_transaction_receipt(tx_hash)

        amount_usdc_received = uniswap_router.functions.getAmountsOut(amount_to_swap, [UNI_ADDRESS, USDC_ADDRESS]).call()[-1]
        amount_usdc_received /= 10**6
        print(f'Received {amount_usdc_received} USDC for {amount_to_swap} UNI')

        return tx_hash, amount_usdc_received
    except Exception as e:
        print(f'Error performing swap on Uniswap: {e}')
        return None, 0

def swap_usdc_to_uni(amount_usdc):
    try:
        amount_usdc_wei = int(amount_usdc * 10**6)
        allowance = check_allowance(usdc, web3.eth.default_account, UNISWAP_V2_ROUTER_ADDRESS)
        balance = check_balance(usdc, web3.eth.default_account)
        if allowance < amount_usdc_wei:
            approve_tx_hash = approve_token(usdc, amount_usdc_wei)
            if approve_tx_hash is None:
                print('USDC approval failed.')
                return None
        if balance < amount_usdc_wei:
            print(f'Insufficient USDC balance. Current balance: {balance}, required: {amount_usdc_wei}')
            return None

        deadline = int(time.time()) + 300
        nonce = web3.eth.get_transaction_count(web3.eth.default_account)
        amount_out_min = 0

        tx = uniswap_router.functions.swapExactTokensForTokens(
            amount_usdc_wei,
            amount_out_min,
            [USDC_ADDRESS, UNI_ADDRESS],
            web3.eth.default_account,
            deadline
        ).build_transaction({
            'from': web3.eth.default_account,
            'nonce': nonce,
            'gas': 300000,
            'gasPrice': web3.eth.gas_price + GAS_PRICE_INCREMENT
        })
        signed_tx = web3.eth.account.sign_transaction(tx, private_key=os.getenv('PRIVATE_KEY'))
        tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
        return tx_hash
    except Exception as e:
        print(f'Error performing swap on Uniswap: {e}')
        return None

def calculate_statistics(prices):
    variance = np.var(prices)
    return variance

def calculate_rsi(prices, period=14):
    if len(prices) < period:
        return None
    delta = np.diff(prices)
    gain = np.maximum(delta, 0)
    loss = -np.minimum(delta, 0)
    avg_gain = np.mean(gain[:period])
    avg_loss = np.mean(loss[:period])
    rs = avg_gain / avg_loss if avg_loss != 0 else 0
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_macd(prices, slow=26, fast=12, signal=9):
    if len(prices) < slow:
        return None, None
    slow_ema = pd.Series(prices).ewm(span=slow).mean()
    fast_ema = pd.Series(prices).ewm(span=fast).mean()
    macd = fast_ema - slow_ema
    signal_line = macd.ewm(span=signal).mean()
    return macd, signal_line

def calculate_linear_regression_slope(prices):
    if len(prices) < 2:
        return None
    X = np.arange(len(prices)).reshape(-1, 1)
    y = np.array(prices).reshape(-1, 1)
    model = LinearRegression()
    model.fit(X, y)
    slope = model.coef_[0][0]
    return slope

def calculate_frequency(price_history, threshold=0.03):
    price_changes = np.diff(price_history) / price_history[:-1]
    frequency = np.sum(np.abs(price_changes) >= threshold) / len(price_changes)
    return frequency

def should_trade(price_history):
    if len(price_history) < PRICE_HISTORY_LENGTH:
        return False
    current_prices = price_history[-PRICE_HISTORY_LENGTH:]
    current_rsi = calculate_rsi(current_prices)
    current_macd, current_signal_line = calculate_macd(current_prices)
    if current_rsi is None or current_macd is None or current_signal_line is None:
        return False
    current_slope = calculate_linear_regression_slope(current_prices)
    variance = calculate_statistics(current_prices)
    if variance > THRESHOLD_VARIANCE and current_slope > LINEAR_REGRESSION_THRESHOLD:
        return True
    return False

def main():
    try:
        while True:
            price_history = []
            while len(price_history) < PRICE_HISTORY_LENGTH:
                current_price = web3.eth.get_average_block_time()
                price_history.append(current_price)
                if should_trade(price_history):
                    tx_hash, amount_usdc_received = swap_uni_to_usdc(AMOUNT_TO_TRADE_UNI)
                    if tx_hash:
                        print(f'Successful trade. Transaction hash: {tx_hash}. Amount received: {amount_usdc_received} USDC.')
                        c.execute('''INSERT INTO transactions (timestamp, tx_hash, amount_usdc, direction) 
                                     VALUES (?, ?, ?, ?)''', (int(time.time()), tx_hash.hex(), amount_usdc_received, 'UNI to USDC'))
                        conn.commit()
                    else:
                        print('Trade failed.')
                time.sleep(60)
            price_history.pop(0)
    except KeyboardInterrupt:
        print('Exiting trading bot.')

if __name__ == '__main__':
    main()
