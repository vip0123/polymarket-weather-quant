"""Check 0x3a25ee contract type to determine correct sigType."""
from curl_cffi import requests as cffi_req

proxy = 'http://MVYNT82F:APS0TC2P@82.38.13.169:46280'
rpc = 'https://polygon-bor-rpc.publicnode.com'
WALLET = '0x3a25ee569eEa47D8Dc547EA3F85B34a371274562'
proxies = {'http': proxy, 'https': proxy}

slots = {
    'slot0': '0x0000000000000000000000000000000000000000000000000000000000000000',
    'eip1967': '0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc',
}
for name, slot in slots.items():
    payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'eth_getStorageAt', 'params': [WALLET, slot, 'latest']}
    r = cffi_req.post(rpc, json=payload, impersonate='chrome', proxies=proxies, timeout=15).json()
    val = r.get('result', '?')
    addr = '0x' + val[-40:] if val and val != '0x' else val
    print(f"{name} => {addr}")

# code length
payload2 = {'jsonrpc': '2.0', 'id': 2, 'method': 'eth_getCode', 'params': [WALLET, 'latest']}
r2 = cffi_req.post(rpc, json=payload2, impersonate='chrome', proxies=proxies, timeout=15).json()
code = r2.get('result', '0x')
print(f"code length: {len(code)} chars ({(len(code)-2)//2} bytes)")

# VERSION() - Gnosis Safe has this
payload3 = {'jsonrpc': '2.0', 'id': 3, 'method': 'eth_call', 'params': [{'to': WALLET, 'data': '0xffa1ad74'}, 'latest']}
r3 = cffi_req.post(rpc, json=payload3, impersonate='chrome', proxies=proxies, timeout=15).json()
raw = r3.get('result', '')
if raw and raw != '0x' and 'error' not in r3:
    # decode as string
    try:
        data = bytes.fromhex(raw[2:])
        # ABI-encoded string: offset (32 bytes) + length (32 bytes) + data
        if len(data) >= 96:
            str_len = int.from_bytes(data[32:64], 'big')
            version_str = data[64:64+str_len].decode('utf-8', errors='replace')
            print(f"VERSION() = '{version_str}'")
        else:
            print(f"VERSION() raw = {raw}")
    except Exception as e:
        print(f"VERSION() decode error: {e}, raw={raw}")
else:
    print(f"VERSION() => {r3}")

# Also try isValidSignature selector - Gnosis Safe has 0x1626ba7e
payload4 = {'jsonrpc': '2.0', 'id': 4, 'method': 'eth_call',
            'params': [{'to': WALLET, 'data': '0x1626ba7e' + '0' * 128}, 'latest']}
r4 = cffi_req.post(rpc, json=payload4, impersonate='chrome', proxies=proxies, timeout=15).json()
print(f"isValidSignature() => {r4}")
