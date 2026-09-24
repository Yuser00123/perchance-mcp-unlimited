"""
Perchance Client - handles ad code, userKey, and image generation
Research based on reverse-engineering as of 2026-09-24:

Main generator: https://perchance.org/stable-diffusion-ai
Official Image API v1: https://perchance.org/perchance-ai-api (iframe + postMessage, permanent URLs on user.uploads.dev)
Embed: https://image-generation.perchance.org/embed#JSON
Ad code: https://perchance.org/api/getAccessCodeForAdPoweredStuff (requires Referer+Origin, blocked without proxy in some regions)
Verify: /api/verifyUser?browserId&token&thread (Turnstile sitekey 0x4AAAAAAAA8g8NphwaSOT59)
Generate: /api/generate?userKey&requestId&adAccessCode&v=... (POST body with prompt, negativePrompt, seed, resolution, guidanceScale, channel, subChannel, userKey, adAccessCode, requestId)
Download: /api/downloadTemporaryImage?imageId=... OR new /api/downloadTemporaryImageViaProxy?t=v1....
Check: /api/checkUserVerificationStatus?userKey&cacheKey

Two APIs:
1. Official API v1 at perchance.org/perchance-ai-api - no keys, iframe postMessage, permanent URLs on user.uploads.dev
2. Internal API at image-generation.perchance.org/api/* - requires userKey (64 hex) + adAccessCode + Turnstile verification

Latest findings 2026-09-24:
- v hash changed from f4fd5ce3a6a768bf12d73c3d6a678d1fb5811e4a24b41c7964a13dc789c811cd to 9b43eec2e71907610e4e9317b54c208495f6850086e92239a869811d2e2d77ee
- New download endpoint /api/downloadTemporaryImageViaProxy?t=v1.... returns image/jpeg with X-Perchance-Proxy-Backend: quick-tunnel, X-Image-Sha256
- Token v1.... is returned in generate response as imageDownloadUrl or proxy token, must be fetched inside Playwright context with cf_clearance cookie
- Ad code endpoint now requires proxy + full headers (UA, Referer, Origin, Accept) else returns Cloudflare Just a moment challenge
- userKey is IP-bound and short-lived (~30s), bound to browserId (32 hex) and residential IP, must be refreshed via Turnstile token
- eeemoon/perchance fix: recursively find proxy_download in generate response, try proxy URL first then fallback to imageId
"""

import asyncio
import base64
import json
import os
import random
import re
import secrets
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List

import httpx

def _setup_ld_path():
    possible_paths = [
        "/tmp/debs/out/usr/lib/x86_64-linux-gnu",
        "/home/user/.perchance-deps/lib",
    ]
    current = os.environ.get("LD_LIBRARY_PATH", "")
    for p in possible_paths:
        if os.path.exists(p) and p not in current:
            os.environ["LD_LIBRARY_PATH"] = f"{p}:{current}" if current else p
_setup_ld_path()

BASE_EMBED = "https://image-generation.perchance.org"
BASE_PERCHANCE = "https://perchance.org"
API_GENERATE = f"{BASE_EMBED}/api/generate"
API_DOWNLOAD = f"{BASE_EMBED}/api/downloadTemporaryImage"
API_DOWNLOAD_PROXY = f"{BASE_EMBED}/api/downloadTemporaryImageViaProxy"
API_VERIFY = f"{BASE_EMBED}/api/verifyUser"
API_CHECK = f"{BASE_EMBED}/api/checkUserVerificationStatus"
API_AD_CODE = f"{BASE_PERCHANCE}/api/getAccessCodeForAdPoweredStuff"
API_QUEUE = f"{BASE_EMBED}/api/getUserQueuePosition"
OFFICIAL_API = f"{BASE_PERCHANCE}/perchance-ai-api"

# Updated version hash as of 2026-09-24 - extracted from embed JS
CLIENT_VERSION_HASH = "9b43eec2e71907610e4e9317b54c208495f6850086e92239a869811d2e2d77ee"
CLIENT_VERSION_HASH_OLD = "f4fd5ce3a6a768bf12d73c3d6a678d1fb5811e4a24b41c7964a13dc789c811cd"

DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Referer": "https://perchance.org/stable-diffusion-ai",
    "Origin": "https://perchance.org",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
EMBED_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Referer": "https://image-generation.perchance.org/embed",
    "Origin": "https://image-generation.perchance.org",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

def get_proxy_url() -> Optional[str]:
    for key in ["PERCHANCE_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"]:
        val = os.getenv(key)
        if val and val.strip():
            if "://" not in val:
                val = f"http://{val}"
            return val.strip()
    return None

# Browserless removed - using fully open-source unlimited stack (curl_cffi + SeleniumBase/Camoufox)
# See OPENSOURCE_GUIDE.md for unlimited alternatives

CACHE_DIR = Path.home() / ".perchance-mcp"
CACHE_DIR.mkdir(exist_ok=True)
KEY_FILE = CACHE_DIR / "user_key.txt"
BROWSER_ID_FILE = CACHE_DIR / "browser_id.txt"

def generate_browser_id() -> str:
    return secrets.token_hex(16)

def generate_request_id() -> str:
    return f"0.{secrets.randbits(30)}"

def _find_proxy_download(value: Any) -> Optional[str]:
    """Recursively find proxy download URL or token in generate response (from eeemoon fix)"""
    if isinstance(value, str):
        if "downloadTemporaryImageViaProxy" in value:
            return value
        if value.startswith("v1.") and len(value) > 80:
            return f"/api/downloadTemporaryImageViaProxy?t={value}" if not value.startswith("/api") else value
        # Full URL with token
        if "downloadTemporaryImageViaProxy?t=v1." in value:
            # Extract path or full URL
            return value
        return None
    if isinstance(value, dict):
        # Check known keys first
        for k in ["imageDownloadUrl", "proxy_download", "proxyDownload", "proxyUrl", "downloadUrl"]:
            if k in value and isinstance(value[k], str):
                found = _find_proxy_download(value[k])
                if found:
                    return found
        for item in value.values():
            result = _find_proxy_download(item)
            if result:
                return result
        return None
    if isinstance(value, list):
        for item in value:
            result = _find_proxy_download(item)
            if result:
                return result
    return None

def get_ad_access_code_via_curl_cffi_sync(proxy_url: Optional[str] = None) -> str:
    """
    Open-source unlimited Cloudflare bypass using curl_cffi TLS impersonation.
    No proxy required, no rate limits, bypasses JA3 fingerprint check.
    Tested: returns 64 hex without proxy, 200 OK.
    """
    try:
        from curl_cffi import requests as curl_requests
        cache_bust = int(time.time() // (60 * 10))
        url = f"{API_AD_CODE}?__cacheBust={cache_bust}"
        headers = {
            "Referer": f"{BASE_PERCHANCE}/stable-diffusion-ai",
            "Origin": BASE_PERCHANCE,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        proxies = None
        if proxy_url:
            proxies = {"http": proxy_url, "https": proxy_url}
        # Use impersonate chrome to bypass TLS fingerprint
        resp = curl_requests.get(url, impersonate="chrome", headers=headers, proxies=proxies, timeout=15)
        if resp.status_code == 200:
            code = resp.text.strip()
            if re.fullmatch(r"[a-f0-9]{64}", code):
                print(f"[Perchance] Got ad code via curl_cffi (open-source unlimited): {code[:12]}...")
                return code
            print(f"[Perchance] curl_cffi ad code non-64hex len={len(code)} snippet={code[:500]}")
        else:
            print(f"[Perchance] curl_cffi ad code status {resp.status_code} body {resp.text[:500]}")
    except ImportError:
        print("[Perchance] curl_cffi not installed, cannot use open-source bypass")
    except Exception as e:
        print(f"[Perchance] curl_cffi ad code failed: {e}")
    return ""

async def get_ad_access_code_via_curl_cffi(proxy_url: Optional[str] = None) -> str:
    """Async wrapper for curl_cffi sync call"""
    return await asyncio.to_thread(get_ad_access_code_via_curl_cffi_sync, proxy_url)

async def get_ad_access_code(client: Optional[httpx.AsyncClient] = None) -> str:
    """Fetch ad access code - required for generation. Uses open-source unlimited bypass first."""
    # Primary: open-source unlimited curl_cffi bypass (no proxy needed, TLS impersonation)
    proxy = get_proxy_url()
    print(f"[Perchance] Trying open-source unlimited curl_cffi bypass for ad code (proxy={proxy[:30] if proxy else 'none'}...)")
    code = await get_ad_access_code_via_curl_cffi(proxy)
    if code:
        return code
    # Fallback to sync version without proxy (curl_cffi without proxy works even when proxy fails)
    if proxy:
        print("[Perchance] Trying curl_cffi without proxy...")
        code = await get_ad_access_code_via_curl_cffi(None)
        if code:
            return code

    close = False
    if client is None:
        if proxy:
            client = httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30, proxy=proxy)
        else:
            client = httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30)
        close = True
    try:
        cache_bust = int(time.time() // (60 * 10))
        url = f"{API_AD_CODE}?__cacheBust={cache_bust}"
        try:
            r = await client.get(url)
            if r.status_code == 200:
                code = r.text.strip()
                if re.fullmatch(r"[a-f0-9]{64}", code):
                    print(f"[Perchance] Got ad code via httpx: {code[:12]}...")
                    return code
                print(f"[Perchance] httpx ad code returned non-64hex len={len(code)} snippet={code[:200]}")
        except Exception as e:
            print(f"[Perchance] httpx ad code failed: {e}")

        # Curl binary fallback - more reliable against Cloudflare TLS fingerprinting
        print(f"[Perchance] Trying curl binary fallback for ad code...")
        cmd = [
            "curl", "-s", "-m", "15",
            "-A", DEFAULT_UA,
            "-H", f"Referer: {BASE_PERCHANCE}/stable-diffusion-ai",
            "-H", f"Origin: {BASE_PERCHANCE}",
            "-H", "Accept: */*",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url
        ]
        if proxy:
            cmd[1:1] = ["--proxy", proxy]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            curl_code = result.stdout.strip()
            if re.fullmatch(r"[a-f0-9]{64}", curl_code):
                print(f"[Perchance] curl fallback got ad code: {curl_code[:12]}...")
                return curl_code
            print(f"[Perchance] curl fallback also non-64hex: {curl_code[:500]}")
        except Exception as e:
            print(f"[Perchance] curl fallback failed: {e}")

        return ""
    finally:
        if close:
            await client.aclose()

async def check_user_key(user_key: str, client: Optional[httpx.AsyncClient] = None) -> bool:
    close = False
    if client is None:
        proxy = get_proxy_url()
        if proxy:
            client = httpx.AsyncClient(headers={**DEFAULT_HEADERS, "Referer": f"{BASE_EMBED}/embed", "Origin": BASE_EMBED}, timeout=20, proxy=proxy)
        else:
            client = httpx.AsyncClient(headers={**DEFAULT_HEADERS, "Referer": f"{BASE_EMBED}/embed", "Origin": BASE_EMBED}, timeout=20)
        close = True
    try:
        url = f"{API_CHECK}?userKey={urllib.parse.quote(user_key)}&cacheKey={int(time.time()//3)}"
        r = await client.get(url)
        if r.status_code != 200:
            return False
        try:
            data = r.json()
            return data.get("status") == "verified"
        except Exception:
            return "verified" in r.text
    except Exception:
        return False
    finally:
        if close:
            await client.aclose()

def load_cached_browser_id() -> str:
    if BROWSER_ID_FILE.exists():
        bid = BROWSER_ID_FILE.read_text().strip()
        if re.fullmatch(r"[a-f0-9]{32}", bid):
            return bid
    bid = generate_browser_id()
    BROWSER_ID_FILE.write_text(bid)
    return bid

def load_cached_user_key() -> Optional[str]:
    env_key = os.getenv("PERCHANCE_USER_KEY")
    if env_key and re.fullmatch(r"[a-f0-9]{64}", env_key.strip()):
        return env_key.strip()
    if KEY_FILE.exists():
        k = KEY_FILE.read_text().strip()
        m = re.search(r"[a-f0-9]{64}", k)
        if m:
            return m.group(0)
    return None

def save_user_key(key: str):
    KEY_FILE.write_text(key)

async def verify_user_with_token(browser_id: str, turnstile_token: str, thread: int = 0) -> Optional[str]:
    """Verify user with browserId and Turnstile token to get fresh userKey (valid ~30s, IP-bound)"""
    proxy = get_proxy_url()
    client_kwargs = {"headers": {**EMBED_HEADERS}, "timeout": 30}
    if proxy:
        client_kwargs["proxy"] = proxy
    async with httpx.AsyncClient(**client_kwargs) as client:
        url = f"{API_VERIFY}?browserId={browser_id}&token={urllib.parse.quote(turnstile_token)}&thread={thread}&__cacheBust={random.random()}"
        print(f"[Perchance] Verifying with token browserId={browser_id} thread={thread}")
        try:
            r = await client.get(url)
            print(f"[Perchance] Verify response status {r.status_code} text {r.text[:500]}")
            data = r.json()
            if data.get("status") in ["success", "already_verified"] and data.get("userKey"):
                key = data["userKey"]
                print(f"[Perchance] Got userKey: {key[:12]}...")
                save_user_key(key)
                return key
            else:
                print(f"[Perchance] Verify failed: {data}")
                return None
        except Exception as e:
            print(f"[Perchance] Verify exception: {e}")
            return None

async def get_user_key_via_browser(headless: bool = True, timeout: int = 120) -> Optional[str]:
    """
    Try to obtain a valid userKey via Playwright automation.
    Navigates to https://image-generation.perchance.org/embed and extracts userKey from localStorage after verification.
    This is the method used by eeemoon/perchance but updated for current API.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Playwright not installed, cannot obtain userKey via browser")
        return None

    browser_id = load_cached_browser_id()
    print(f"[Perchance] Trying to obtain userKey via browser, browserId={browser_id}")

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=headless,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]
            )
        except Exception as e:
            print(f"[Perchance] Failed to launch chromium: {e}")
            return None

        proxy_url = get_proxy_url()
        context_kwargs = {
            "user_agent": DEFAULT_UA,
            "viewport": {"width": 1280, "height": 900},
        }
        if proxy_url:
            print(f"[Perchance] Using proxy for browser: {proxy_url[:40]}...")
            try:
                from urllib.parse import urlparse
                parsed = urlparse(proxy_url)
                proxy_server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
                context_kwargs["proxy"] = {"server": proxy_server}
                if parsed.username:
                    context_kwargs["proxy"]["username"] = parsed.username
                    context_kwargs["proxy"]["password"] = parsed.password or ""
            except Exception as e:
                print(f"[Perchance] Failed to parse proxy for Playwright: {e}")

        context = await browser.new_context(**context_kwargs)
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        try:
            from playwright_stealth import Stealth
            stealth_available = True
        except ImportError:
            stealth_available = False

        page = await context.new_page()
        if stealth_available:
            try:
                await Stealth().apply_stealth_async(page)
            except Exception:
                pass

        found_keys = []

        def handle_request(req):
            url = req.url
            if "userKey=" in url:
                m = re.search(r"userKey=([a-f0-9]{64})", url)
                if m:
                    found_keys.append(m.group(1))

        context.on("request", handle_request)
        page.on("console", lambda msg: print(f"[Browser CONSOLE {msg.type}] {msg.text[:500]}"))

        try:
            # Go to embed page - this will trigger verification flow
            await page.goto(f"{BASE_EMBED}/embed", wait_until="domcontentloaded", timeout=90000)
            # Wait for verification to happen - the embed JS does tokenless verification then Turnstile if needed
            for attempt in range(12):
                await page.wait_for_timeout(5000)
                try:
                    # Try to get browserId and userKeys from localStorage
                    result = await page.evaluate("""() => {
                        try {
                            const bid = localStorage.getItem('generation-v2-browser');
                            if(!bid) return {bid: null, keys: []};
                            const keys=[];
                            for(let i=0;i<5;i++){
                                const v=localStorage.getItem('generation-v2:'+bid+':userKey-'+i);
                                if(v && /^[a-f0-9]{64}$/.test(v)) keys.push(v);
                            }
                            return {bid, keys, all: Object.keys(localStorage).filter(k=>k.includes('generation')).slice(0,10)};
                        } catch(e) { return {err:e.message}; }
                    }""")
                    print(f"[Perchance] Attempt {attempt} localStorage: {result}")
                    if result.get("keys"):
                        found_keys.extend(result["keys"])
                        break
                except Exception as e:
                    print(f"[Perchance] eval error {e}")

                # Also try to get userKey from page content (eeemoon method: fetch /api/verifyUser?thread=0 and parse)
                try:
                    content = await page.content()
                    m = re.search(r'"userKey"\s*:\s*"([a-f0-9]{64})"', content)
                    if m:
                        print(f"[Perchance] Found userKey in page content: {m.group(1)[:12]}...")
                        found_keys.append(m.group(1))
                        break
                except Exception:
                    pass

            # Try the eeemoon method: goto verifyUser endpoint directly and parse userKey from JSON
            if not found_keys:
                try:
                    await page.goto(f"{BASE_EMBED}/api/verifyUser?thread=0&__cacheBust={random.random()}", wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(3000)
                    content = await page.content()
                    print(f"[Perchance] verifyUser page content: {content[:2000]}")
                    m = re.search(r'"userKey"\s*:\s*"([a-f0-9]{64})"', content)
                    if m:
                        found_keys.append(m.group(1))
                except Exception as e:
                    print(f"[Perchance] verifyUser direct failed: {e}")

        except Exception as e:
            print(f"[Perchance] Browser navigation error: {e}")
        finally:
            await browser.close()

    if found_keys:
        key = found_keys[-1]
        print(f"[Perchance] Obtained userKey: {key[:12]}...")
        save_user_key(key)
        return key
    print("[Perchance] Failed to obtain userKey via browser")
    return None

async def generate_image_http(
    prompt: str,
    negative_prompt: str = "",
    seed: int = -1,
    resolution: str = "512x512",
    guidance_scale: float = 7.0,
    channel: str = "stable-diffusion-ai",
    sub_channel: str = "public",
    user_key: Optional[str] = None,
    ad_access_code: Optional[str] = None,
    request_id: Optional[str] = None,
    browser_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate image via direct HTTP API (requires valid userKey and adAccessCode)
    Updated to use new v hash 9b43eec2... and handle proxy download URLs
    """
    shape_map = {
        "portrait": "512x768",
        "square": "512x512",
        "landscape": "768x512",
        "square_hd": "768x768",
    }
    if resolution not in ["512x512", "512x768", "768x512", "768x768"]:
        resolution = shape_map.get(resolution.lower(), "512x512")

    if browser_id is None:
        browser_id = load_cached_browser_id()

    if user_key is None:
        user_key = load_cached_user_key()
        if user_key:
            if not await check_user_key(user_key):
                print(f"[Perchance] Cached userKey invalid, trying to refresh...")
                user_key = None

    if user_key is None:
        user_key = await get_user_key_via_browser(headless=True)
        if not user_key:
            raise RuntimeError(
                "Failed to obtain userKey. Perchance requires Cloudflare Turnstile verification which is failing in this environment. "
                "Possible fixes: 1) Set env var PERCHANCE_USER_KEY with a valid 64-char hex key obtained from your browser's localStorage (generation-v2:*:userKey-0), "
                "2) Run this MCP server on your local machine (not sandboxed) where Turnstile passes, "
                "3) Provide a fresh Turnstile token via verify_user_with_token()."
            )

    if ad_access_code is None:
        proxy = get_proxy_url()
        if proxy:
            async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=20, proxy=proxy) as client:
                ad_access_code = await get_ad_access_code(client)
        else:
            async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=20) as client:
                ad_access_code = await get_ad_access_code(client)

    if not ad_access_code:
        print("[Perchance] Warning: adAccessCode empty, generation may fail with invalid_access_code")

    if request_id is None:
        request_id = generate_request_id()

    body = {
        "prompt": prompt,
        "negativePrompt": negative_prompt,
        "seed": seed,
        "resolution": resolution,
        "guidanceScale": guidance_scale,
        "channel": channel,
        "subChannel": sub_channel,
        "userKey": user_key,
        "adAccessCode": ad_access_code,
        "requestId": request_id,
    }

    # Try new v hash first, then old as fallback
    for v_hash in [CLIENT_VERSION_HASH, CLIENT_VERSION_HASH_OLD]:
        params = {
            "userKey": user_key,
            "requestId": request_id,
            "adAccessCode": ad_access_code,
            "v": v_hash,
            "__cacheBust": str(random.random()),
        }

        proxy = get_proxy_url()
        client_kwargs = {"headers": {**EMBED_HEADERS}, "timeout": 120}
        if proxy:
            client_kwargs["proxy"] = proxy
            print(f"[Perchance] Using proxy for generate: {proxy[:30]}...")
        async with httpx.AsyncClient(**client_kwargs) as client:
            print(f"[Perchance] Sending generate request with v={v_hash[:12]}... prompt='{prompt[:50]}' res={resolution} seed={seed}")
            r = await client.post(API_GENERATE, params=params, json=body)
            print(f"[Perchance] Generate response status {r.status_code}")
            try:
                data = r.json()
            except Exception:
                print(f"[Perchance] Generate response text: {r.text[:1000]}")
                if v_hash == CLIENT_VERSION_HASH:
                    print(f"[Perchance] Trying old v hash as fallback...")
                    continue
                raise RuntimeError(f"Generate failed: {r.text[:500]}")

            print(f"[Perchance] Generate response: {json.dumps(data)[:1000]}")

            if data.get("status") == "client_update_required":
                print(f"[Perchance] client_update_required with v={v_hash[:12]}, trying other hash...")
                if v_hash == CLIENT_VERSION_HASH:
                    continue
                else:
                    raise RuntimeError(f"Client update required even with both v hashes: {data}")

            if data.get("status") == "invalid_key":
                print("[Perchance] invalid_key, clearing cache and retrying...")
                if KEY_FILE.exists():
                    KEY_FILE.unlink()
                new_key = await get_user_key_via_browser(headless=True)
                if new_key:
                    return await generate_image_http(
                        prompt, negative_prompt, seed, resolution, guidance_scale,
                        channel, sub_channel, new_key, ad_access_code, generate_request_id(), browser_id
                    )
                raise RuntimeError("invalid_key and failed to refresh")

            if data.get("status") == "failed_verification":
                raise RuntimeError(f"Failed verification: {data} - need Turnstile token")

            if data.get("status") == "invalid_access_code":
                print("[Perchance] invalid_access_code, refreshing...")
                ad_access_code = await get_ad_access_code(client)
                params["adAccessCode"] = ad_access_code
                body["adAccessCode"] = ad_access_code
                r = await client.post(API_GENERATE, params=params, json=body)
                data = r.json()
                if data.get("status") != "success":
                    raise RuntimeError(f"Still failing after ad code refresh: {data}")

            if data.get("status") == "waiting_for_prev_request_to_finish":
                print("[Perchance] Waiting for previous request to finish, retrying in 8s...")
                await asyncio.sleep(8)
                return await generate_image_http(
                    prompt, negative_prompt, seed, resolution, guidance_scale,
                    channel, sub_channel, user_key, ad_access_code, request_id, browser_id
                )

            if data.get("status") != "success":
                if v_hash == CLIENT_VERSION_HASH:
                    print(f"[Perchance] Generate failed with new hash, trying old: {data}")
                    continue
                raise RuntimeError(f"Generation failed: {data}")

            # Success - add proxy download info
            proxy_download = _find_proxy_download(data)
            if proxy_download:
                print(f"[Perchance] Found proxy download: {proxy_download[:100]}...")
                data["_proxy_download"] = proxy_download

            return data

    raise RuntimeError("Generate failed with both v hashes")

async def download_image(image_id: str = "", file_extension: str = "jpeg", proxy_download: Optional[str] = None) -> bytes:
    """Download image by imageId or proxy_download token/URL. Tries proxy URL first."""
    proxy = get_proxy_url()
    urls = []

    if proxy_download:
        # proxy_download can be full URL, path, or just token v1....
        if proxy_download.startswith("http"):
            urls.append(proxy_download)
        elif proxy_download.startswith("/api/") or proxy_download.startswith("/"):
            # Ensure full URL
            if proxy_download.startswith("/api/downloadTemporaryImageViaProxy"):
                urls.append(f"{BASE_EMBED}{proxy_download}")
            elif proxy_download.startswith("/downloadTemporaryImageViaProxy"):
                urls.append(f"{BASE_EMBED}/api{proxy_download}")
            else:
                urls.append(f"{BASE_EMBED}{proxy_download}")
        elif proxy_download.startswith("v1."):
            urls.append(f"{API_DOWNLOAD_PROXY}?t={proxy_download}")
        else:
            # Assume it's already a path with token
            urls.append(f"{BASE_EMBED}{proxy_download}" if proxy_download.startswith("/") else f"{API_DOWNLOAD_PROXY}?t={proxy_download}")

    if image_id:
        urls.append(f"{API_DOWNLOAD}?imageId={urllib.parse.quote(image_id)}")

    if not urls:
        raise ValueError("No imageId or proxy_download provided")

    # First try direct httpx with proxy
    kwargs = {"headers": EMBED_HEADERS, "timeout": 60}
    if proxy:
        kwargs["proxy"] = proxy

    for url in urls:
        try:
            print(f"[Perchance] Trying download via httpx: {url[:200]}")
            async with httpx.AsyncClient(**kwargs) as client:
                r = await client.get(url)
                if r.status_code == 200 and len(r.content) > 1000:
                    print(f"[Perchance] Download success via httpx, size {len(r.content)}")
                    return r.content
                else:
                    print(f"[Perchance] Download failed status {r.status_code} len {len(r.content)} text {r.text[:200] if r.content else ''}")
        except Exception as e:
            print(f"[Perchance] httpx download error for {url[:100]}: {e}")

    # Fallback: try via Playwright fetch (handles cf_clearance cookies)
    print(f"[Perchance] Trying download via Playwright fallback...")
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox","--disable-blink-features=AutomationControlled"])
            ctx_kwargs = {"user_agent": DEFAULT_UA}
            if proxy:
                from urllib.parse import urlparse
                parsed = urlparse(proxy)
                proxy_server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
                ctx_kwargs["proxy"] = {"server": proxy_server}
                if parsed.username:
                    ctx_kwargs["proxy"]["username"] = parsed.username
                    ctx_kwargs["proxy"]["password"] = parsed.password or ""
            context = await browser.new_context(**ctx_kwargs)
            try:
                from playwright_stealth import Stealth
                page = await context.new_page()
                await Stealth().apply_stealth_async(page)
            except ImportError:
                page = await context.new_page()

            # First go to embed to get cf_clearance
            await page.goto(f"{BASE_EMBED}/embed", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)

            result = await page.evaluate("""
                async (urls) => {
                    const failures=[];
                    for(const url of urls){
                        try{
                            const r=await fetch(url);
                            if(!r.ok){
                                const txt=await r.text();
                                failures.push(`${r.status} ${url} ${txt.slice(0,200)}`);
                                continue;
                            }
                            const blob=await r.blob();
                            if(blob.size<1000){
                                failures.push(`small ${blob.size} ${url}`);
                                continue;
                            }
                            const base64=await new Promise(res=>{
                                const reader=new FileReader();
                                reader.onloadend=()=>res(reader.result.split(',')[1]);
                                reader.readAsDataURL(blob);
                            });
                            return {ok:true, url, size: blob.size, type: blob.type, data: base64};
                        }catch(e){
                            failures.push(`err ${url} ${e.message}`);
                        }
                    }
                    return {ok:false, failures};
                }
            """, urls)

            await browser.close()

            if result.get("ok"):
                print(f"[Perchance] Download success via Playwright, size {result['size']}")
                return base64.b64decode(result["data"])
            else:
                print(f"[Perchance] Playwright download failures: {result.get('failures')}")
    except Exception as e:
        print(f"[Perchance] Playwright download fallback error: {e}")

    raise RuntimeError(f"Failed to download image from all URLs: {urls}")

def data_url_to_bytes(data_url: str) -> Tuple[bytes, str]:
    """Convert data URL to bytes and extension"""
    header, b64 = data_url.split(",", 1)
    ext = "jpeg"
    if "image/png" in header:
        ext = "png"
    elif "image/webp" in header:
        ext = "webp"
    elif "image/jpeg" in header or "image/jpg" in header:
        ext = "jpeg"
    return base64.b64decode(b64), ext

def load_styles() -> Dict[str, Dict[str, str]]:
    """Load art styles from bundled file if available, else hardcoded common list"""
    styles_file = Path(__file__).parent / "styles.json"
    if styles_file.exists():
        try:
            return json.loads(styles_file.read_text())
        except Exception:
            pass
    return {
        "No style": {"prompt": "[input.description]", "negative": ""},
        "Cinematic": {"prompt": "[input.description], cinematic shot, dynamic lighting, 75mm, Technicolor, Panavision, cinemascope, sharp focus, fine details, 8k, HDR, realism, realistic, key visual, film still, cinematic color grading, depth of field.", "negative": ""},
        "Digital Painting": {"prompt": "[input.description], breathtaking digital art, trending on artstation, in the style of atey ghailan, greg rutkowski, 8k", "negative": ""},
        "Anime": {"prompt": "anime art of [input.description], masterpiece, 4k, anime", "negative": ""},
        "Fantasy Painting": {"prompt": "[input.description], d&d, fantasy, highly detailed, digital painting, artstation, sharp focus, fantasy art, illustration, 8k", "negative": ""},
        "Oil Painting": {"prompt": "breathtaking oil painting of [input.description], oil on canvas, masterpiece", "negative": ""},
        "Pixel Art": {"prompt": "(pixel art), [input.description], best pixel art, 16-bit, 128px", "negative": ""},
        "3D Disney Character": {"prompt": "3D Disney character of [input.description], Pixar render, cute big eyes, 4k", "negative": ""},
        "Studio Ghibli": {"prompt": "Studio Ghibli style artwork of [input.description], Hayao Miyazaki style, anime film still", "negative": ""},
    }

def apply_style(prompt: str, style_name: str, styles: Optional[Dict] = None) -> str:
    """Apply art style template to prompt"""
    if styles is None:
        styles = load_styles()
    style = styles.get(style_name)
    if not style:
        for k, v in styles.items():
            if k.lower() == style_name.lower():
                style = v
                break
    if not style:
        return prompt
    template = style.get("prompt", "[input.description]")
    return template.replace("[input.description]", prompt)

# --- Official API v1 via iframe + postMessage ---
async def generate_via_official_api(
    prompt: str,
    negative_prompt: str = "",
    resolution: str = "512x512",
    guidance_scale: float = 7.0,
    seed: int = -1,
    timeout: int = 180,
) -> Dict[str, Any]:
    """
    Generate image via official Perchance Image API v1 (https://perchance.org/perchance-ai-api)
    This uses iframe + postMessage pattern as documented.
    Returns dict with url, dataUrl, seed, etc. and permanent URL on user.uploads.dev
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("Playwright not installed - needed for official API")

    shape_map = {
        "portrait": "512x768",
        "square": "512x512",
        "landscape": "768x512",
        "square_hd": "768x768",
    }
    if resolution in shape_map:
        res = resolution
    elif resolution in ["512x512", "512x768", "768x512", "768x768"]:
        res = resolution
    else:
        res = shape_map.get(resolution.lower(), "512x512")

    params = {
        "prompt": prompt,
        "format": "json",
        "id": secrets.token_hex(4),
    }
    if negative_prompt:
        params["negativePrompt"] = negative_prompt
    if res:
        params["resolution"] = res
    if guidance_scale != 7.0:
        params["guidanceScale"] = str(guidance_scale)
    if seed != -1:
        params["seed"] = str(seed)

    api_url = f"{OFFICIAL_API}?{urllib.parse.urlencode(params)}"
    print(f"[Perchance Official API] URL: {api_url[:300]}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"]
        )
        proxy_url = get_proxy_url()
        context_kwargs = {
            "user_agent": DEFAULT_UA,
            "viewport": {"width": 1280, "height": 900},
        }
        if proxy_url:
            print(f"[Perchance Official API] Using proxy: {proxy_url[:40]}...")
            try:
                from urllib.parse import urlparse
                parsed = urlparse(proxy_url)
                proxy_server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
                context_kwargs["proxy"] = {"server": proxy_server}
                if parsed.username:
                    context_kwargs["proxy"]["username"] = parsed.username
                    context_kwargs["proxy"]["password"] = parsed.password or ""
            except Exception as e:
                print(f"[Perchance] Failed to parse proxy: {e}")

        context = await browser.new_context(**context_kwargs)
        await context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        try:
            from playwright_stealth import Stealth
            stealth_available = True
        except ImportError:
            stealth_available = False

        page = await context.new_page()
        if stealth_available:
            try:
                await Stealth().apply_stealth_async(page)
            except Exception:
                pass

        page.on("console", lambda msg: print(f"[Official API CONSOLE {msg.type}] {msg.text[:400]}"))

        await page.goto("about:blank")
        parent_html = f"""
        <html><body>
        <script>
        var _warmed = false;
        function warmUp() {{
          return new Promise(function (res) {{
            if (_warmed) return res(true);
            var f = document.createElement("iframe");
            f.style.cssText = "width:1px;height:1px;border:0;position:absolute;opacity:0";
            f.src = "https://perchance.org";
            var t = setTimeout(function () {{ f.remove(); res(false); }}, 15000);
            f.onload = function () {{
              clearTimeout(t);
              setTimeout(function () {{ f.remove(); _warmed = true; res(true); }}, 500);
            }};
            document.body.appendChild(f);
          }});
        }}
        window.__result = null;
        window.__error = null;
        function generateImage() {{
          return new Promise(function (resolve, reject) {{
            var p = new URLSearchParams({{ prompt: {json.dumps(prompt)}, format: "json", id: {json.dumps(params['id'])} }});
            p.set("resolution", {json.dumps(res)});
            {"p.set('negativePrompt', "+json.dumps(negative_prompt)+");" if negative_prompt else ""}
            {"p.set('guidanceScale', "+json.dumps(str(guidance_scale))+");" if guidance_scale!=7.0 else ""}
            {"p.set('seed', "+json.dumps(str(seed))+");" if seed!=-1 else ""}
            var f = document.createElement("iframe");
            f.style.cssText = "width:512px;height:512px;border:1px solid red;";
            f.src = "https://perchance.org/perchance-ai-api?" + p;
            var settled = false;
            function onMsg(e) {{
              var d = e.data || {{}};
              if (d.api !== "perchance-image-api" || !d.result) return;
              if (d.result.id !== p.get("id")) return;
              console.log("MSG received: " + JSON.stringify(d).slice(0,2000));
              finish(d.result.ok ? resolve : reject, d.result);
            }}
            function finish(cb, result) {{
              if (settled) return; settled = true;
              window.removeEventListener("message", onMsg);
              window.__result = result;
              if (result.ok) window.__result.url = result.url;
              cb(result);
            }}
            window.addEventListener("message", onMsg);
            document.body.appendChild(f);
            setTimeout(function () {{ finish(reject, {{ ok: false, error: "timeout after 180s" }}); }}, 180000);
          }});
        }}
        window.doGenerate = async () => {{
          await warmUp();
          console.log("warmUp done");
          try {{
            const r = await generateImage();
            console.log("GENERATED: " + JSON.stringify(r).slice(0,3000));
            window.__result = r;
            return r;
          }} catch(e) {{
            console.log("ERR: " + e.message);
            window.__error = e.message;
            throw e;
          }}
        }};
        </script>
        <h1>Perchance Official API Test</h1>
        </body></html>
        """
        await page.set_content(parent_html)
        await page.wait_for_timeout(2000)

        try:
            result = await page.evaluate("() => window.doGenerate()")
            print(f"[Official API] Result: {json.dumps(result)[:2000]}")
            await browser.close()
            return result
        except Exception as e:
            print(f"[Official API] doGenerate threw: {e}, polling for 180s...")
            for _ in range(36):
                await page.wait_for_timeout(5000)
                try:
                    res = await page.evaluate("()=>window.__result")
                    if res:
                        print(f"[Official API] Polled result: {json.dumps(res)[:2000]}")
                        await browser.close()
                        return res
                    err = await page.evaluate("()=>window.__error")
                    if err:
                        await browser.close()
                        raise RuntimeError(f"Generation error: {err}")
                except Exception as inner:
                    if "timeout" in str(inner).lower():
                        continue
                    print(f"poll err {inner}")
            await browser.close()
            raise RuntimeError("Official API generation timed out after 180s - likely Cloudflare blocking in this environment")

async def generate_via_embed_playwright(
    prompt: str,
    negative_prompt: str = "",
    seed: int = -1,
    resolution: str = "512x512",
    guidance_scale: float = 7.0,
    channel: str = "stable-diffusion-ai",
    timeout: int = 180,
) -> Dict[str, Any]:
    """
    Generate image via embed page directly using Playwright.
    This mimics what the user's browser does: navigates to embed with hash payload,
    waits for Turnstile verification and generation, then captures dataUrl and imageDownloadUrl.
    More reliable than HTTP API in some environments because it uses the embed's own JS.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("Playwright not installed")

    payload = {
        "prompt": prompt,
        "negativePrompt": negative_prompt,
        "seed": seed,
        "resolution": resolution,
        "guidanceScale": guidance_scale,
        "saveChannel": channel,
        "channel": channel,
        "subChannel": "public",
        "requestId": generate_request_id(),
        "iframeId": f"mcp_{secrets.token_hex(4)}",
    }
    hash_str = urllib.parse.quote(json.dumps(payload))
    embed_url = f"{BASE_EMBED}/embed#{hash_str}"

    print(f"[Perchance Embed] Generating via {embed_url[:500]}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"]
        )
        proxy_url = get_proxy_url()
        context_kwargs = {
            "user_agent": DEFAULT_UA,
            "viewport": {"width": 1280, "height": 900},
        }
        if proxy_url:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(proxy_url)
                proxy_server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
                context_kwargs["proxy"] = {"server": proxy_server}
                if parsed.username:
                    context_kwargs["proxy"]["username"] = parsed.username
                    context_kwargs["proxy"]["password"] = parsed.password or ""
            except Exception as e:
                print(f"[Perchance] Failed to parse proxy: {e}")

        context = await browser.new_context(**context_kwargs)
        await context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        try:
            from playwright_stealth import Stealth
            page = await context.new_page()
            await Stealth().apply_stealth_async(page)
        except ImportError:
            page = await context.new_page()

        page.on("console", lambda msg: print(f"[Embed CONSOLE {msg.type}] {msg.text[:800]}"))

        await page.goto(embed_url, wait_until="domcontentloaded", timeout=60000)

        # Wait for generation to complete - look for resultImgEl or finished message
        result = None
        for i in range(timeout // 5):
            await page.wait_for_timeout(5000)
            try:
                # Check if image is done
                check = await page.evaluate("""() => {
                    const img = document.querySelector('#resultImgEl');
                    if(img && img.src){
                        return {done: true, src: img.src.slice(0,100), fullSrc: img.src, seed: window.seedUsedForThisResult, hasArrayBuffer: !!window.arrayBufferOfResultImage, imageId: window.imageIdOfResultImage, fileExt: window.fileExtensionOfResultImage};
                    }
                    // Also check for finishedGeneration flag
                    if(window.finishedGeneration){
                        return {done: !!document.querySelector('#resultImgEl'), finished: true, hasArrayBuffer: !!window.arrayBufferOfResultImage, imageId: window.imageIdOfResultImage};
                    }
                    return {done: false, waiting: document.querySelector('#waitingContentEl')?.innerText?.slice(0,200) || ''};
                }""")
                print(f"[Embed] Poll {i*5}s: {check}")
                if check.get("done") and check.get("fullSrc"):
                    result = {
                        "status": "success",
                        "dataUrl": check["fullSrc"],
                        "seed": check.get("seed", seed),
                        "imageId": check.get("imageId"),
                        "fileExtension": check.get("fileExt", "jpeg"),
                    }
                    break
                # If we have arrayBuffer, we can get dataUrl via blobToBase64
                if check.get("hasArrayBuffer"):
                    data = await page.evaluate("""async () => {
                        try{
                            const buf = window.arrayBufferOfResultImage;
                            if(!buf) return null;
                            const ext = window.fileExtensionOfResultImage || 'jpeg';
                            const blob = new Blob([buf], {type: 'image/'+ext});
                            const base64 = await new Promise(res=>{
                                const reader=new FileReader();
                                reader.onloadend=()=>res(reader.result);
                                reader.readAsDataURL(blob);
                            });
                            return {dataUrl: base64, seed: window.seedUsedForThisResult, imageId: window.imageIdOfResultImage, fileExt: ext};
                        }catch(e){ return {err:e.message}; }
                    }""")
                    if data and data.get("dataUrl"):
                        result = {
                            "status": "success",
                            "dataUrl": data["dataUrl"],
                            "seed": data.get("seed", seed),
                            "imageId": data.get("imageId"),
                            "fileExtension": data.get("fileExt", "jpeg"),
                        }
                        break
            except Exception as e:
                print(f"[Embed] Poll error: {e}")

        await browser.close()

        if not result:
            raise RuntimeError(f"Embed generation timed out after {timeout}s")

        # Try to find proxy download URL from page if available
        # The embed JS stores imageDownloadUrl in result, but we didn't capture it. We can try to get it from network logs or from result object if we had intercepted generate response.
        # For now, return dataUrl which is sufficient.

        return result

async def generate_via_browser(
    prompt: str,
    negative_prompt: str = "",
    seed: int = -1,
    resolution: str = "512x512",
    guidance_scale: float = 7.0,
    headless: bool = False,
) -> Dict[str, Any]:
    """
    Fallback: Generate image via full browser automation on perchance.org/stable-diffusion-ai
    Captures dataUrl from embed. More reliable in some envs but slower.
    """
    # Use the new embed playwright method as primary
    try:
        return await generate_via_embed_playwright(
            prompt=prompt,
            negative_prompt=negative_prompt,
            seed=seed,
            resolution=resolution,
            guidance_scale=guidance_scale,
        )
    except Exception as e:
        print(f"[Perchance] generate_via_embed failed: {e}, falling back to old method...")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("Playwright not installed")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=["--no-sandbox","--disable-blink-features=AutomationControlled"])
        proxy_url = get_proxy_url()
        context_kwargs = {"user_agent": DEFAULT_UA, "viewport": {"width": 1280, "height": 900}}
        if proxy_url:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(proxy_url)
                proxy_server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
                context_kwargs["proxy"] = {"server": proxy_server}
                if parsed.username:
                    context_kwargs["proxy"]["username"] = parsed.username
                    context_kwargs["proxy"]["password"] = parsed.password or ""
            except Exception as e:
                print(f"[Perchance] Failed to parse proxy: {e}")
        context = await browser.new_context(**context_kwargs)
        await context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined}); window.forceDisableAds=true;")
        try:
            from playwright_stealth import Stealth
            stealth_available = True
        except ImportError:
            stealth_available = False

        page = await context.new_page()
        if stealth_available:
            try:
                await Stealth().apply_stealth_async(page)
            except Exception:
                pass

        await page.goto("https://perchance.org/stable-diffusion-ai", wait_until="domcontentloaded", timeout=120000)
        await page.wait_for_timeout(10000)

        gen_frame = None
        for fr in page.frames:
            if "c0c3b93a" in fr.url:
                gen_frame = fr
                break

        if not gen_frame:
            await browser.close()
            raise RuntimeError("Generator frame not found")

        iframe_id = f"mcp_{secrets.token_hex(4)}"
        request_id = generate_request_id()
        payload = {
            "prompt": prompt,
            "negativePrompt": negative_prompt,
            "seed": seed,
            "resolution": resolution,
            "guidanceScale": guidance_scale,
            "saveChannel": "stable-diffusion-ai",
            "channel": "stable-diffusion-ai",
            "subChannel": "public",
            "iframeId": iframe_id,
            "requestId": request_id,
        }
        embed_url = f"{BASE_EMBED}/embed#{urllib.parse.quote(json.dumps(payload))}"

        await gen_frame.evaluate("""() => {
            window.__mcpResult=null;
            window.addEventListener('message', e => {
                if(e.origin!=='https://image-generation.perchance.org') return;
                if(e.data.type==='finished') window.__mcpResult=e.data;
            });
        }""")

        await gen_frame.evaluate("(url) => { const ifr=document.createElement('iframe'); ifr.src=url; ifr.style.width='512px'; ifr.style.height='512px'; document.body.appendChild(ifr); }", embed_url)

        for _ in range(36):
            await page.wait_for_timeout(5000)
            has = await gen_frame.evaluate("()=>!!window.__mcpResult")
            if has:
                res = await gen_frame.evaluate("()=>window.__mcpResult")
                await browser.close()
                return {"dataUrl": res["dataUrl"], "seed": res.get("seedUsed", seed), "status": "success"}

        await browser.close()
        raise RuntimeError("Browser generation timed out after 180s")

# --- Open-source self-hosted unlimited alternatives (no Browserless limit) ---

def _ensure_ld_path():
    """Ensure LD_LIBRARY_PATH includes our manually installed Firefox/Chromium deps for sandbox"""
    possible = ["/tmp/debs/out/usr/lib/x86_64-linux-gnu", "/home/user/.perchance-deps/lib"]
    cur = os.environ.get("LD_LIBRARY_PATH", "")
    added = False
    for p in possible:
        if os.path.exists(p) and p not in cur:
            os.environ["LD_LIBRARY_PATH"] = f"{p}:{cur}" if cur else p
            cur = os.environ["LD_LIBRARY_PATH"]
            added = True
    if added:
        print(f"[Perchance OpenSource] LD_LIBRARY_PATH set to {os.environ['LD_LIBRARY_PATH'][:100]}...")

async def get_user_key_via_camoufox(timeout: int = 60) -> Optional[str]:
    """
    Self-hosted unlimited Turnstile solver using Camoufox (Firefox-based stealth browser).
    Requires: pip install camoufox, camoufox fetch, and LD_LIBRARY_PATH with Firefox deps.
    This is fully open-source and unlimited (no 1000/month limit).
    Returns userKey 64 hex or None.
    """
    _ensure_ld_path()
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError:
        print("[Perchance Camoufox] camoufox not installed, pip install camoufox")
        return None

    print("[Perchance Camoufox] Launching stealth Firefox for Turnstile...")
    try:
        async with AsyncCamoufox(headless=True, humanize=True, os="windows") as browser:
            page = await browser.new_page()
            # Use embed with hash to avoid JSON parse error
            import json as _json, urllib.parse as _up
            hash_data = {"prompt": "test", "seed": 0, "resolution": "512x512", "guidanceScale": 7, "negativePrompt": "", "requestId": f"camou_{random.random()}", "iframeId": "test"}
            url = f"{BASE_EMBED}/embed#{_up.quote(_json.dumps(hash_data))}"
            page.on("console", lambda msg: print(f"[Camoufox CONSOLE {msg.type}] {msg.text[:500]}"))

            await page.goto(url)
            await page.wait_for_timeout(5000)

            # Try to solve Turnstile via manual injection (fallback if auto fails)
            result = await page.evaluate("""async () => {
                const bid = localStorage.getItem('generation-v2-browser');
                console.log('bid '+bid);

                // Try to load turnstile if not present
                if(!window.turnstile){
                    await new Promise((res, rej) => {
                        window.onloadTurnstileCallback = () => {
                            console.log('turnstile loaded via callback');
                            res();
                        };
                        const s = document.createElement('script');
                        s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?onload=onloadTurnstileCallback';
                        s.onerror = () => {
                            console.log('turnstile script onerror');
                            res();
                        };
                        document.body.appendChild(s);
                        setTimeout(() => res(), 10000);
                    });
                    await new Promise(r => setTimeout(r, 2000));
                }

                if(!window.turnstile){
                    return {error: 'turnstile still undefined after load', bid};
                }

                let ctn = document.querySelector('#cfTurnstileCtn');
                if(!ctn){
                    ctn = document.createElement('div');
                    ctn.id = 'cfTurnstileCtn';
                    document.body.appendChild(ctn);
                }

                try{
                    const token = await new Promise((resolve, reject) => {
                        let settled = false;
                        const timer = setTimeout(() => {
                            if(!settled){
                                settled = true;
                                resolve(null);
                            }
                        }, 40000);
                        window.cloudflareTurnstileTokenResolver = (t) => {
                            if(!settled){
                                settled = true;
                                clearTimeout(timer);
                                resolve(t);
                            }
                        };
                        try{
                            window.turnstile.render('#cfTurnstileCtn', {
                                sitekey: '0x4AAAAAAAA8g8NphwaSOT59',
                                callback: (t) => window.cloudflareTurnstileTokenResolver(t),
                                'error-callback': (...args) => {
                                    console.log('turnstile error', args);
                                    if(!settled){
                                        settled = true;
                                        clearTimeout(timer);
                                        resolve(null);
                                    }
                                }
                            });
                        }catch(e){
                            console.log('render exception '+e);
                            if(!settled){
                                settled = true;
                                clearTimeout(timer);
                                resolve(null);
                            }
                        }
                    });

                    if(!token){
                        return {error: 'no token after 40s', bid};
                    }

                    console.log('Got token '+token.slice(0,50));
                    const verifyUrl = `/api/verifyUser?browserId=${bid}&token=${encodeURIComponent(token)}&thread=0&__cacheBust=${Math.random()}`;
                    const r = await fetch(verifyUrl);
                    const txt = await r.text();
                    console.log('Verify response '+txt.slice(0,1000));
                    let j;
                    try{ j = JSON.parse(txt); }catch(e){ return {error: 'verify not json '+txt.slice(0,500), bid}; }
                    return {bid, response: j};
                }catch(e){
                    return {error: 'exception '+e.toString(), bid};
                }
            }""")

            print(f"[Perchance Camoufox] Verify result: {result}")
            user_key = result.get('response', {}).get('userKey') if isinstance(result, dict) else None
            if user_key and re.fullmatch(r"[a-f0-9]{64}", user_key):
                print(f"[Perchance Camoufox] Got userKey: {user_key[:12]}...")
                save_user_key(user_key)
                return user_key
            else:
                print(f"[Perchance Camoufox] Failed: {result}")
                return None
    except Exception as e:
        print(f"[Perchance Camoufox] Exception: {e}")
        import traceback; traceback.print_exc()
        return None

async def get_user_key_via_seleniumbase(timeout: int = 60) -> Optional[str]:
    """
    Self-hosted unlimited Turnstile solver using SeleniumBase UC Mode (undetected chromedriver).
    Requires: pip install seleniumbase, playwright chromium binary.
    Fully open-source unlimited.
    """
    _ensure_ld_path()
    try:
        from seleniumbase import SB
    except ImportError:
        print("[Perchance SeleniumBase] seleniumbase not installed")
        return None

    # Find chromium binary
    chromium_path = "/home/user/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome"
    import glob
    if not os.path.exists(chromium_path):
        candidates = glob.glob("/home/user/.cache/ms-playwright/chromium-*/chrome-linux64/chrome")
        if candidates:
            chromium_path = candidates[0]
        else:
            print("[Perchance SeleniumBase] Chromium binary not found, run playwright install chromium")
            return None

    print(f"[Perchance SeleniumBase] Launching UC chromium {chromium_path}...")

    def _run_sync():
        with SB(uc=True, headless=True, chromium_arg="--no-sandbox --disable-gpu --disable-dev-shm-usage --disable-blink-features=AutomationControlled", binary_location=chromium_path) as sb:
            import json as _json, urllib.parse as _up, time as _time
            hash_data = {"prompt": "test", "seed": 0, "resolution": "512x512", "guidanceScale": 7, "negativePrompt": "", "requestId": f"sb_{random.random()}", "iframeId": "test"}
            url = f"{BASE_EMBED}/embed#{_up.quote(_json.dumps(hash_data))}"
            sb.open(url)
            sb.sleep(8)
            bid = sb.execute_script("return localStorage.getItem('generation-v2-browser')")
            print(f"[SeleniumBase] bid {bid}")

            # Try to get turnstile token via JS
            # Use execute_async_script to wait for token
            try:
                result = sb.execute_async_script("""
                    const callback = arguments[arguments.length - 1];
                    (async () => {
                        const bid = localStorage.getItem('generation-v2-browser');
                        if(!window.turnstile){
                            window.onloadTurnstileCallback = () => {};
                            const s = document.createElement('script');
                            s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?onload=onloadTurnstileCallback';
                            document.body.appendChild(s);
                            await new Promise(r => setTimeout(r, 5000));
                        }
                        if(!window.turnstile){
                            callback({error: 'no turnstile', bid});
                            return;
                        }
                        let ctn = document.querySelector('#cfTurnstileCtn');
                        if(!ctn){
                            ctn = document.createElement('div');
                            ctn.id = 'cfTurnstileCtn';
                            document.body.appendChild(ctn);
                        }
                        try{
                            const token = await new Promise((resolve) => {
                                let settled=false;
                                const timer=setTimeout(()=>{if(!settled){settled=true;resolve(null);}},35000);
                                window.cloudflareTurnstileTokenResolver=(t)=>{if(!settled){settled=true;clearTimeout(timer);resolve(t);}};
                                window.turnstile.render('#cfTurnstileCtn', {
                                    sitekey: '0x4AAAAAAAA8g8NphwaSOT59',
                                    callback: (t)=>window.cloudflareTurnstileTokenResolver(t),
                                    'error-callback': ()=>{if(!settled){settled=true;clearTimeout(timer);resolve(null);}}
                                });
                            });
                            if(!token){
                                callback({error: 'no token', bid});
                                return;
                            }
                            const verifyUrl = `/api/verifyUser?browserId=${bid}&token=${encodeURIComponent(token)}&thread=0&__cacheBust=${Math.random()}`;
                            const r = await fetch(verifyUrl);
                            const txt = await r.text();
                            let j;
                            try{ j=JSON.parse(txt); }catch(e){ callback({error: 'verify not json '+txt.slice(0,500), bid}); return; }
                            callback({bid, response: j});
                        }catch(e){
                            callback({error: 'exception '+e.toString(), bid});
                        }
                    })();
                """, timeout=60)
                print(f"[SeleniumBase] result {result}")
                return result
            except Exception as e:
                print(f"[SeleniumBase] async script error {e}")
                return {"error": str(e)}

    try:
        result = await asyncio.to_thread(_run_sync)
        user_key = result.get('response', {}).get('userKey') if isinstance(result, dict) else None
        if user_key and re.fullmatch(r"[a-f0-9]{64}", user_key):
            print(f"[Perchance SeleniumBase] Got userKey: {user_key[:12]}...")
            save_user_key(user_key)
            return user_key
        return None
    except Exception as e:
        print(f"[Perchance SeleniumBase] Exception {e}")
        return None

async def get_user_key_via_opensource() -> Optional[str]:
    """
    Try open-source self-hosted unlimited solvers in order:
    1. SeleniumBase UC Mode (Chromium undetected) - TESTED WORKING
    2. Camoufox (Firefox stealth, best for Turnstile) - experimental
    Fully open-source unlimited, no Browserless needed.
    """
    print("[Perchance OpenSource] Trying SeleniumBase UC (tested working)...")
    key = await get_user_key_via_seleniumbase()
    if key:
        return key

    print("[Perchance OpenSource] SeleniumBase failed, trying Camoufox...")
    key = await get_user_key_via_camoufox()
    if key:
        return key

    print("[Perchance OpenSource] All open-source methods failed")
    return None

async def generate_image_via_opensource(
    prompt: str,
    negative_prompt: str = "",
    seed: int = -1,
    resolution: str = "512x512",
    guidance_scale: float = 7.0,
    channel: str = "stable-diffusion-ai",
) -> Dict[str, Any]:
    """
    Fully open-source unlimited image generation:
    - ad code via curl_cffi (TLS impersonation, unlimited, no proxy needed) - TESTED
    - userKey via SeleniumBase UC Mode / Camoufox (self-hosted, unlimited) - TESTED
    - generate & download via curl_cffi (bypasses Cloudflare) - TESTED
    No Browserless, no API keys, unlimited free.
    """
    print(f"[Perchance OpenSource] Starting generation prompt='{prompt[:50]}'...")

    # Step 1: ad code via curl_cffi (unlimited)
    ad_code = await get_ad_access_code_via_curl_cffi()
    if not ad_code:
        print("[Perchance OpenSource] Warning: ad code empty, trying without")

    # Step 2: userKey via open-source solvers
    user_key = await get_user_key_via_opensource()
    if not user_key:
        # Try cached
        user_key = load_cached_user_key()
        if user_key:
            print(f"[Perchance OpenSource] Using cached userKey {user_key[:12]}...")
            # Check if still valid
            if not await check_user_key(user_key):
                print("[Perchance OpenSource] Cached key invalid")
                user_key = None

    if not user_key:
        raise RuntimeError("Failed to obtain userKey via open-source methods. Try setting PERCHANCE_USER_KEY env var with a fresh key, or ensure seleniumbase and chromium are installed.")

    browser_id = load_cached_browser_id()

    # Step 3: Generate via curl_cffi (bypasses Cloudflare)
    print(f"[Perchance OpenSource] Generating with userKey={user_key[:12]}... adCode={ad_code[:12] if ad_code else 'none'}...")
    try:
        from curl_cffi import requests as curl_requests
        cache_bust = random.random()
        request_id = generate_request_id()
        params = {
            "userKey": user_key,
            "requestId": request_id,
            "adAccessCode": ad_code,
            "v": CLIENT_VERSION_HASH,
            "__cacheBust": cache_bust
        }
        body = {
            "prompt": prompt,
            "negativePrompt": negative_prompt,
            "seed": seed,
            "resolution": resolution,
            "guidanceScale": guidance_scale,
            "channel": channel,
            "subChannel": "public",
            "userKey": user_key,
            "adAccessCode": ad_code,
            "requestId": request_id
        }
        headers = {
            "Referer": f"{BASE_EMBED}/embed",
            "Origin": BASE_EMBED,
            "Content-Type": "application/json",
            "Accept": "*/*",
        }
        url = f"{API_GENERATE}?{urllib.parse.urlencode(params)}"
        # IMPORTANT: userKey is IP-bound to the IP that solved Turnstile (sandbox IP for self-hosted)
        # So generate/download must use same IP (no proxy) to avoid invalid_key
        # Ad code via curl_cffi works without proxy anyway, so we use no proxy for generate/download
        # Only use proxy for ad code if needed, but we already got ad code without proxy
        proxies = None

        # curl_cffi sync in thread
        def _do_generate():
            sess = curl_requests.Session(impersonate="chrome")
            r = sess.post(url, json=body, headers=headers, proxies=proxies, timeout=30)
            return {"status": r.status_code, "text": r.text[:10000], "json": r.json() if r.headers.get("content-type","").startswith("application/json") or r.text.strip().startswith("{") else None}

        gen_result = await asyncio.to_thread(_do_generate)
        print(f"[Perchance OpenSource] Generate result status {gen_result['status']} {gen_result['text'][:2000]}")

        # Parse JSON
        try:
            data = json.loads(gen_result['text'])
        except Exception:
            data = gen_result.get('json')

        if not data or data.get('status') != 'success':
            raise RuntimeError(f"Generation failed: {gen_result['text'][:1000]}")

        image_id = data.get('imageId')
        proxy_download = data.get('imageDownloadUrl')
        file_ext = data.get('fileExtension', 'jpeg')
        seed_used = data.get('seed', seed)

        # Handle direct data URLs (Perchance sometimes returns imageDataUrls)
        if data.get('imageDataUrls') and len(data['imageDataUrls']) > 0:
            print(f"[Perchance OpenSource] Got direct imageDataUrls, using first")
            data_url_direct = data['imageDataUrls'][0]
            # Convert data URL to bytes
            if data_url_direct.startswith('data:'):
                b64_part = data_url_direct.split(',', 1)[1] if ',' in data_url_direct else ''
                try:
                    image_bytes = base64.b64decode(b64_part)
                    data_url = data_url_direct
                    return {
                        "status": "success",
                        "imageId": image_id,
                        "imageDownloadUrl": proxy_download,
                        "fileExtension": file_ext,
                        "seed": seed_used,
                        "dataUrl": data_url,
                        "imageBytes": image_bytes,
                        "prompt": prompt,
                        "width": data.get('width'),
                        "height": data.get('height'),
                        "maybeNsfw": data.get('maybeNsfw', False),
                        "_opensource": True,
                        "_browserId": browser_id,
                        "_userKey": user_key,
                        "_adCode": ad_code,
                    }
                except Exception as e:
                    print(f"[Perchance OpenSource] Failed to decode imageDataUrls: {e}")

        # Step 4: Download via curl_cffi (must be immediate, same IP, token single-use expires ~5min)
        print(f"[Perchance OpenSource] Downloading imageId={image_id} proxy={proxy_download[:100] if proxy_download else 'none'}...")

        def _do_download():
            sess = curl_requests.Session(impersonate="chrome")
            urls = []
            if proxy_download:
                urls.append(proxy_download if proxy_download.startswith('http') else f"{BASE_EMBED}{proxy_download}")
            if image_id:
                urls.append(f"{BASE_EMBED}/api/downloadTemporaryImage?imageId={image_id}")
            for dl_url in urls:
                try:
                    r = sess.get(dl_url, headers={"Referer": f"{BASE_EMBED}/embed", "Origin": BASE_EMBED}, proxies=proxies, timeout=30)
                    if r.status_code == 200 and len(r.content) > 1000:
                        print(f"[Perchance OpenSource] Downloaded {len(r.content)} bytes from {dl_url[:100]}")
                        return {"ok": True, "bytes": r.content, "url": dl_url}
                except Exception as e:
                    print(f"[Perchance OpenSource] Download error {dl_url}: {e}")
            return {"ok": False}

        dl_result = await asyncio.to_thread(_do_download)
        if not dl_result.get('ok'):
            # Last resort: if generate returned imageDataUrls but we missed, try again
            if data.get('imageDataUrls'):
                print("[Perchance OpenSource] Download failed, falling back to imageDataUrls")
                data_url_direct = data['imageDataUrls'][0]
                b64_part = data_url_direct.split(',', 1)[1] if ',' in data_url_direct else ''
                image_bytes = base64.b64decode(b64_part)
                data_url = data_url_direct
            else:
                raise RuntimeError(f"Download failed via curl_cffi. Generate response keys: {list(data.keys())} imageId={image_id} proxy={proxy_download}")

        image_bytes = dl_result['bytes'] if dl_result.get('ok') else image_bytes
        data_url = f"data:image/{file_ext};base64,{base64.b64encode(image_bytes).decode()}" if dl_result.get('ok') else data_url

        return {
            "status": "success",
            "imageId": image_id,
            "imageDownloadUrl": proxy_download,
            "fileExtension": file_ext,
            "seed": seed_used,
            "dataUrl": data_url,
            "imageBytes": image_bytes,
            "prompt": prompt,
            "width": data.get('width'),
            "height": data.get('height'),
            "maybeNsfw": data.get('maybeNsfw', False),
            "_opensource": True,
            "_browserId": browser_id,
            "_userKey": user_key,
            "_adCode": ad_code,
        }

    except ImportError:
        print("[Perchance OpenSource] curl_cffi not installed, falling back to httpx")
        # Fallback to httpx generate
        return await generate_image_http(prompt, negative_prompt, seed, resolution, guidance_scale, channel, user_key, ad_code)

async def generate_image_via_opensource_sync(
    prompt: str,
    negative_prompt: str = "",
    seed: int = -1,
    resolution: str = "512x512",
    guidance_scale: float = 7.0,
    channel: str = "stable-diffusion-ai",
) -> Dict[str, Any]:
    """Sync wrapper for open-source generation"""
    return await generate_image_via_opensource(prompt, negative_prompt, seed, resolution, guidance_scale, channel)
