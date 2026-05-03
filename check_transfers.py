"""Check recent USDC transfer events for Polymarket wallet addresses."""
import os, httpx
from dotenv import load_dotenv
load_dotenv('.env')
proxy_url = os.environ.get('POLY_PROXY_URL', '')

rpc = 'https://polygon-bor-rpc.publicnode.com'
usdc_e = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174'
usdc_n = '0x3c499c542cef5e3811e1192ce70d8cc03d5c3359'

TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'

addrs = {
    'EOA     ': '0xC8f40ECF82794cC75C95566e171F29C68e5a6FD1',
    'V1 proxy': '0x3a25ee569eEa47D8Dc547EA3F85B34a371274562',
    'V2 proxy': '0x5b88123faba45602c9bc115d4f9d92e6f7f630d9',
}

def pad(addr):
    return '0x' + addr[2:].lower().zfill(64)

with httpx.Client(proxy=proxy_url, timeout=20) as c:
    r = c.post(rpc, json={'jsonrpc':'2.0','method':'eth_blockNumber','params':[],'id':1})
    cur_block = int(r.json()['result'], 16)
    from_block = hex(cur_block - 100000)  # ~48h
    print(f'Current block: {cur_block}  scanning last 100k blocks\n')

    for label, addr in addrs.items():
        print(f'--- {label} {addr} ---')
        for direction, topic_idx in [('IN', 2), ('OUT', 1)]:
            topics = [TRANSFER, None, None]
            topics[topic_idx] = pad(addr)
            for token_label, token in [('USDC.e', usdc_e), ('USDC', usdc_n)]:
                resp = c.post(rpc, json={'jsonrpc':'2.0','method':'eth_getLogs','params':[{
                    'fromBlock': from_block,
                    'toBlock': 'latest',
                    'address': token,
                    'topics': topics
                }],'id':1}, timeout=20)
                logs = resp.json().get('result', [])
                for log in logs[-5:]:
                    amount = int(log['data'], 16) / 1e6
                    from_addr = '0x' + log['topics'][1][-40:]
                    to_addr = '0x' + log['topics'][2][-40:]
                    blk = int(log['blockNumber'], 16)
                    print(f'  [{token_label}] {direction} {amount:.4f}  from={from_addr[:12]}  to={to_addr[:12]}  block={blk}')
        print()
