"""Check Polymarket portfolio balance for all wallet addresses."""
import os, httpx
from dotenv import load_dotenv
load_dotenv('.env')
proxy_url = os.environ.get('POLY_PROXY_URL', '')

eoa = '0xC8f40ECF82794cC75C95566e171F29C68e5a6FD1'
v1  = '0x3a25ee569eEa47D8Dc547EA3F85B34a371274562'
v2  = '0x5b88123faba45602c9bc115d4f9d92e6f7f630d9'

with httpx.Client(proxy=proxy_url, timeout=15) as c:
    for label, addr in [('EOA', eoa), ('V1', v1), ('V2', v2)]:
        for ep_label, url in [
            ('value',     f'https://data-api.polymarket.com/value?user={addr}'),
            ('portfolio', f'https://gamma-api.polymarket.com/portfolio?user={addr}'),
            ('positions', f'https://data-api.polymarket.com/positions?user={addr}&sizeThreshold=0.01'),
        ]:
            try:
                r = c.get(url)
                txt = r.text[:300]
                if r.status_code == 200 and txt not in ('[]', 'null', '{}', ''):
                    print(f'{label} {ep_label} ({addr[:10]}): {txt}')
                else:
                    print(f'{label} {ep_label} ({addr[:10]}): {r.status_code} {txt[:60]}')
            except Exception as e:
                print(f'{label} {ep_label}: ERROR {e}')
        print()
