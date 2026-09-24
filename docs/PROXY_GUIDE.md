# Proxy Support for Perchance MCP

## Why Proxy?

Perchance uses Cloudflare Turnstile with error 600010 for bot detection. In datacenter IPs (like E2B sandbox IP 35.197.91.170), Turnstile fails:

```
CONS error: ❌ Turnstile error callback fired: 600010
```

This prevents obtaining fresh `userKey` via browser automation.

Also, `userKey` appears to be short-lived (30 sec `recentlyVerified` window) and possibly IP-bound. Your key `0bf2c46542ee28caa73c0576ff8482b5e1ebd94f8312be1f64e7fe62d5654248` is canonical for browserId `fa2e9a2af105eca0e4f15ea2b7b35bc1` (re-issued after each successful verification), but shows `not_verified` / `invalid_key` from datacenter IP after expiry.

## Proxy Solution

The MCP server now supports proxy via env var:

```bash
export PERCHANCE_PROXY="http://user:pass@host:port"
# or
export PERCHANCE_PROXY="socks5://host:port"
# or standard env vars:
export HTTP_PROXY="http://host:port"
export HTTPS_PROXY="http://host:port"
```

Supported in:
- `httpx` for `get_ad_access_code`, `check_user_key`, `generate_image_http`, `download_image`
- `playwright` for `get_user_key_via_browser`, `generate_via_official_api`, `generate_via_browser`

### Testing Proxy

```bash
cd /home/user/perchance-mcp
export LD_LIBRARY_PATH=/tmp/debs/out/usr/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH
export PERCHANCE_PROXY="http://45.86.231.96:4000"  # Example working proxy
python3 - << 'PY'
import httpx, asyncio, os
os.environ['PERCHANCE_PROXY'] = "http://45.86.231.96:4000"
from client import get_proxy_url, check_user_key
print(f"Proxy: {get_proxy_url()}")
async def test():
    valid = await check_user_key("0bf2c46542ee28caa73c0576ff8482b5e1ebd94f8312be1f64e7fe62d5654248")
    print(f"is_verified: {valid}")
asyncio.run(test())
PY
```

### Residential vs Datacenter

- **Datacenter proxies** (Tencent Cloud 43.128.109.40, Alibaba, etc.) - still fail Turnstile 600010, still show `not_verified` for expired key
- **Residential proxies** (Comcast, Cox, Verizon, mobile LTE) - more likely to pass Turnstile and make userKey valid

We tested:
- `43.128.109.40:8081` (Tencent Cloud) - works for httpbin IP check, but `checkUserVerificationStatus` still `not_verified`, `generate` still `invalid_key`
- `45.86.231.96:4000` - works for IP check, but same `not_verified`
- `184.181.217.220:4145` (Cox Residential) - server disconnected (not working)

### Recommended: Free Residential Proxy Sources

1. **Webshare** - 10 free proxies, some residential: https://www.webshare.io/
2. **ProxyScrape** with elite filter: https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=1000&anonymity=elite
3. **Geonode** with residential ISP filter: https://proxylist.geonode.com/api/proxy-list?limit=50&filterUpTime=90
4. **Use your own mobile phone as proxy** - apps like "Proxy Server" on Android can create HTTP proxy on your LTE IP (residential/mobile)

### How to Get Fresh Key Quickly (30 sec window)

Your key `0bf2c...` reappears after manual edit + refresh + generate because Perchance re-issues same key for same browserId after Turnstile verification. So it's canonical, but needs fresh verification timestamp.

To get fresh valid key:
1. Open https://perchance.org/stable-diffusion-ai on your phone (LTE, residential IP)
2. Generate an image
3. **Immediately** (within 5 sec) go to DevTools > Local Storage > https://image-generation.perchance.org > generation-v2:fa2e9a2af105eca0e4f15ea2b7b35bc1:userKey-0
4. Copy the 64-char value
5. Paste here - I'll test `generate` within seconds before it expires

If you have a residential proxy, provide it as `PERCHANCE_PROXY` and I can try to re-verify browserId `fa2e9a2af105eca0e4f15ea2b7b35bc1` via browser automation with proxy - Turnstile should pass with residential IP and give fresh userKey.

### MCP Config with Proxy

```json
{
  "mcpServers": {
    "perchance": {
      "command": "python",
      "args": ["/home/user/perchance-mcp/server.py"],
      "env": {
        "PERCHANCE_USER_KEY": "fresh-64-char",
        "PERCHANCE_PROXY": "http://user:pass@residential-host:port",
        "LD_LIBRARY_PATH": "/tmp/debs/out/usr/lib/x86_64-linux-gnu"
      }
    }
  }
}
```

### Current Status

- browserId: fa2e9a2af105eca0e4f15ea2b7b35bc1 (from your screenshot)
- userKey: 0bf2c46542ee28caa73c0576ff8482b5e1ebd94f8312be1f64e7fe62d5654248 (canonical for this browserId, but expired/not_verified from datacenter)
- adAccessCode: works (3a18ffcbf5de4690fd7aa5608c60280bc9e91a2da891550950912fcb476b4f34)
- Playwright with xvfb headed + stealth: gets to Turnstile but fails 600010 in datacenter
- With residential proxy: should pass Turnstile and get fresh verification

Try providing a residential proxy or fresh key!
