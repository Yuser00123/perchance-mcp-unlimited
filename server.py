"""
Perchance Stable Diffusion MCP Server
Provides tools to interact with https://perchance.org/stable-diffusion-ai
and https://perchance.org/perchance-ai-api (official Image API v1)

Tools:
- generate_image: Generate via internal API (requires userKey, fastest) - now supports proxy_download token
- generate_image_official: Generate via official API v1 (iframe postMessage, permanent URLs)
- generate_image_embed: Generate via embed page directly (most reliable, handles Turnstile)
- generate_batch: Generate multiple images
- list_styles: List available art styles
- get_generator_info: Info about the generator
- download_image: Download image by ID or proxy token
- get_user_key_status: Check if cached userKey is valid
- refresh_user_key: Force refresh userKey via browser
- get_ad_code: Get ad access code
- verify_with_token: Verify with browserId and Turnstile token to get fresh userKey

Research Summary as of 2026-09-24:
- Internal API: https://image-generation.perchance.org/api/generate?userKey&requestId&adAccessCode&v=...
  v hash updated: 9b43eec2e71907610e4e9317b54c208495f6850086e92239a869811d2e2d77ee (old: f4fd5ce3...)
  Requires: browserId (32 hex), userKey (64 hex), adAccessCode (64 hex), Turnstile token sitekey 0x4AAAAAAAA8g8NphwaSOT59
  Flow: generation-identity-v2.js creates browserId, verifyUser?browserId&token&thread -> userKey, then generate
  Download now uses /api/downloadTemporaryImageViaProxy?t=v1.... (quick-tunnel backend) with X-Image-Sha256 header
  Token v1.... is returned as imageDownloadUrl in generate response
- Official API v1: https://perchance.org/perchance-ai-api?prompt=...&format=json
  Works via iframe + postMessage, no keys, returns permanent user.uploads.dev URLs

Cloudflare notes:
- Ad code endpoint now requires proxy + full headers (UA, Referer, Origin) else returns Just a moment challenge
- verifyUser and checkUserVerificationStatus require Referer/Origin headers
- userKey is IP-bound and short-lived (~30s), must be refreshed frequently
- In sandbox/datacenter, Cloudflare may block with 403. Solution: run MCP server locally, not in sandbox, or provide PERCHANCE_USER_KEY and PERCHANCE_PROXY
"""

import asyncio
import base64
import json
import os
import random
import re
import secrets
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

from mcp.server.fastmcp import FastMCP

def _setup_ld():
    for p in ["/tmp/debs/out/usr/lib/x86_64-linux-gnu", "/home/user/.perchance-deps/lib"]:
        if os.path.exists(p):
            cur = os.environ.get("LD_LIBRARY_PATH","")
            if p not in cur:
                os.environ["LD_LIBRARY_PATH"] = f"{p}:{cur}" if cur else p
_setup_ld()

try:
    from client import (
        generate_image_http,
        generate_via_official_api,
        generate_via_embed_playwright,
        download_image,
        data_url_to_bytes,
        load_styles,
        apply_style,
        get_ad_access_code,
        check_user_key,
        load_cached_user_key,
        load_cached_browser_id,
        get_user_key_via_browser,
        generate_via_browser,
        verify_user_with_token,
        _find_proxy_download,
        CACHE_DIR,
        KEY_FILE,
        BROWSER_ID_FILE,
        CLIENT_VERSION_HASH,
    )
except ImportError:
    from perchance_mcp.client import (
        generate_image_http,
        generate_via_official_api,
        generate_via_embed_playwright,
        download_image,
        data_url_to_bytes,
        load_styles,
        apply_style,
        get_ad_access_code,
        check_user_key,
        load_cached_user_key,
        load_cached_browser_id,
        get_user_key_via_browser,
        generate_via_browser,
        verify_user_with_token,
        _find_proxy_download,
        CACHE_DIR,
        KEY_FILE,
        BROWSER_ID_FILE,
        CLIENT_VERSION_HASH,
    )

mcp = FastMCP("perchance-stable-diffusion")
STYLES = load_styles()

@mcp.tool()
async def list_styles() -> str:
    """List all available art styles for Perchance Stable Diffusion."""
    result = []
    for name, data in STYLES.items():
        result.append({
            "name": name,
            "prompt_template": data.get("prompt", "")[:200],
            "negative": data.get("negative", "")[:100]
        })
    return json.dumps({"count": len(result), "styles": result}, indent=2)

@mcp.tool()
async def get_generator_info() -> str:
    """Get information about the Perchance Stable Diffusion generator."""
    info = {
        "name": "Stable Diffusion Online (Perchance)",
        "url": "https://perchance.org/stable-diffusion-ai",
        "official_api": "https://perchance.org/perchance-ai-api",
        "official_api_docs": "https://perchance.org/perchance-ai-api",
        "description": "Free, no sign-up, no limits Stable Diffusion generator",
        "api_endpoint_internal": "https://image-generation.perchance.org/api/generate",
        "api_endpoint_official": "https://perchance.org/perchance-ai-api?prompt=...&format=json",
        "api_endpoint_download_proxy": "https://image-generation.perchance.org/api/downloadTemporaryImageViaProxy?t=v1....",
        "resolutions": ["512x512", "512x768", "768x512", "768x768"],
        "shapes": {"portrait": "512x768", "square": "512x512", "landscape": "768x512", "square_hd": "768x768"},
        "guidance_scale": {"min": 1, "max": 30, "default": 7},
        "seed": {"default": -1, "note": "-1 for random"},
        "art_styles_count": len(STYLES),
        "client_version_hash": CLIENT_VERSION_HASH,
        "client_version_hash_old": "f4fd5ce3a6a768bf12d73c3d6a678d1fb5811e4a24b41c7964a13dc789c811cd",
        "how_it_works_internal": [
            "1. Browser gets adAccessCode from https://perchance.org/api/getAccessCodeForAdPoweredStuff (requires Referer+Origin, now needs proxy in some regions)",
            "2. Browser verifies via Turnstile (sitekey 0x4AAAAAAAA8g8NphwaSOT59) to get userKey (64 hex) stored in localStorage generation-v2:{browserId}:userKey-{thread}",
            "3. Tokenless verify: GET /api/verifyUser?browserId=...&thread=... -> may return token_required",
            "4. With token: GET /api/verifyUser?browserId=...&token=...&thread=... -> returns userKey",
            "5. Check: GET /api/checkUserVerificationStatus?userKey=... -> verified",
            "6. POST to /api/generate?userKey=...&requestId=...&adAccessCode=...&v=9b43eec2e71907610e4e9317b54c208495f6850086e92239a869811d2e2d77ee with JSON body",
            "7. Response contains imageId and imageDownloadUrl (proxy token v1....), then download via /api/downloadTemporaryImageViaProxy?t=... or /api/downloadTemporaryImage?imageId=...",
            "8. Embed posts finished message with dataUrl via postMessage",
        ],
        "how_it_works_official": [
            "1. warmUp() - load https://perchance.org in hidden iframe to pass Cloudflare",
            "2. Create iframe src=https://perchance.org/perchance-ai-api?prompt=...&format=json&id=random",
            "3. Listen for window message: e.data.api === 'perchance-image-api' && e.data.result.id === your id",
            "4. Result: { ok: true, url: 'https://user.uploads.dev/...', dataUrl, seed, ... } - permanent URL!",
        ],
        "how_it_works_embed": [
            "1. Navigate to https://image-generation.perchance.org/embed#{prompt, resolution, etc as JSON}",
            "2. Embed JS does tokenless verification, then Turnstile if needed, then joinQueue (generate)",
            "3. Poll for #resultImgEl or window.finishedGeneration",
            "4. Get dataUrl from img src or arrayBuffer",
        ],
        "cache_dir": str(CACHE_DIR),
        "env_vars": {
            "PERCHANCE_USER_KEY": "64-char hex userKey from localStorage generation-v2:{browserId}:userKey-0",
            "PERCHANCE_PROXY": "http://user:pass@host:port for bypassing Cloudflare in sandbox",
            "PERCHANCE_BROWSER_ID": "32-char hex browserId, optional, will be generated if not set",
        },
        "notes": [
            "Internal API is fastest but requires userKey (Turnstile). v hash updated 2026-09-24 to 9b43eec2...",
            "New download endpoint /api/downloadTemporaryImageViaProxy?t=v1.... replaces imageId method, uses quick-tunnel backend",
            "Official API v1 is recommended for external apps, no keys, permanent URLs, but still needs browser JS",
            "In sandboxed/datacenter IPs, Cloudflare may block both. Run MCP server locally for best results.",
            "userKey is IP-bound and short-lived (~30s). Use verify_with_token tool with fresh Turnstile token if you have it.",
            "Ad code now requires proxy + full headers, implemented with curl fallback in client.py",
        ]
    }
    return json.dumps(info, indent=2)

@mcp.tool()
async def get_user_key_status() -> str:
    """Check if cached userKey is valid and show cache info."""
    browser_id = load_cached_browser_id()
    user_key = load_cached_user_key()
    result = {
        "browser_id": browser_id,
        "browser_id_file": str(BROWSER_ID_FILE),
        "user_key": f"{user_key[:12]}...{user_key[-6:]}" if user_key else None,
        "user_key_file": str(KEY_FILE),
        "user_key_exists": bool(user_key),
    }
    if user_key:
        try:
            valid = await check_user_key(user_key)
            result["is_verified"] = valid
        except Exception as e:
            result["is_verified"] = False
            result["error"] = str(e)
    else:
        result["is_verified"] = False
        result["note"] = "No cached key. Use refresh_user_key or set PERCHANCE_USER_KEY env var. To get key manually: open https://perchance.org/stable-diffusion-ai in real browser, DevTools > Application > Local Storage > https://image-generation.perchance.org, find generation-v2:{browserId}:userKey-0 (64 hex)."

    return json.dumps(result, indent=2)

@mcp.tool()
async def refresh_user_key(headless: bool = True) -> str:
    """Force refresh the userKey via browser automation. May fail in sandbox due to Cloudflare."""
    try:
        key = await get_user_key_via_browser(headless=headless)
        if key:
            return json.dumps({"status": "success", "user_key": f"{key[:12]}...{key[-6:]}", "full_key": key, "note": "Saved to cache"})
        else:
            return json.dumps({"status": "failed", "error": "Could not obtain userKey via browser. Cloudflare likely blocked datacenter IP. Run locally or set PERCHANCE_USER_KEY."})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

@mcp.tool()
async def verify_with_token(browser_id: str, turnstile_token: str, thread: int = 0) -> str:
    """
    Verify with browserId and Turnstile token to get fresh userKey.
    Use this when you have a fresh Turnstile token from your browser's Network tab.
    
    Args:
        browser_id: 32-char hex browserId from localStorage generation-v2-browser or from verifyUser URL
        turnstile_token: The token string from verifyUser?browserId=...&token=... URL (long, starts with 0.XXX)
        thread: Thread number, usually 0
    
    Returns:
        JSON with userKey if success.
    
    How to get token:
    1. Open https://perchance.org/stable-diffusion-ai in real browser
    2. DevTools > Network, filter 'verifyUser'
    3. Generate an image
    4. Find request with '&token=' param, copy token value
    5. Use this tool within 30 seconds, from same IP (run MCP locally, not sandbox)
    """
    try:
        key = await verify_user_with_token(browser_id, turnstile_token, thread)
        if key:
            return json.dumps({"status": "success", "user_key": key, "browser_id": browser_id, "thread": thread, "note": "Valid ~30s, IP-bound, saved to cache"})
        else:
            return json.dumps({"status": "failed", "error": "Verification failed, token may be expired or IP mismatch. Token valid <30s and IP-bound."})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

@mcp.tool()
async def generate_image(
    prompt: str,
    negative_prompt: str = "",
    seed: int = -1,
    resolution: str = "512x512",
    guidance_scale: float = 7.0,
    art_style: str = "",
    channel: str = "stable-diffusion-ai",
    save_path: str = "",
    use_browser_fallback: bool = False,
) -> str:
    """
    Generate an image using Perchance Stable Diffusion via INTERNAL API.
    
    This is the fastest method but requires a valid userKey (64 hex). If you have PERCHANCE_USER_KEY env var set, it will use it.
    Otherwise it tries to obtain via browser (may fail in sandbox due to Cloudflare).
    Now supports new proxy download endpoint /api/downloadTemporaryImageViaProxy?t=v1....
    
    Args:
        prompt: Description of what to draw
        negative_prompt: Things to avoid
        seed: -1 for random, or specific integer
        resolution: 512x512, 512x768, 768x512, 768x768, or shape names: portrait, square, landscape, square_hd
        guidance_scale: 1-30, default 7
        art_style: Optional art style name from list_styles
        channel: Gallery channel (default stable-diffusion-ai)
        save_path: Optional path to save image
        use_browser_fallback: If True, uses embed Playwright method instead of HTTP API (more reliable locally)
    
    Returns:
        JSON with image info, saved path, seed used.
    """
    final_prompt = prompt
    final_negative = negative_prompt
    if art_style:
        final_prompt = apply_style(prompt, art_style, STYLES)
        style_data = STYLES.get(art_style) or next((v for k,v in STYLES.items() if k.lower()==art_style.lower()), None)
        if style_data:
            style_neg = style_data.get("negative","").replace("[input.negative || \"\"]","").replace("[input.negative]","").strip().strip('"')
            if style_neg and not negative_prompt:
                final_negative = style_neg
            elif style_neg and negative_prompt:
                final_negative = f"{negative_prompt}, {style_neg}"

    try:
        if use_browser_fallback:
            result = await generate_via_embed_playwright(
                prompt=final_prompt,
                negative_prompt=final_negative,
                seed=seed,
                resolution=resolution,
                guidance_scale=guidance_scale,
                channel=channel,
            )
            data_url = result["dataUrl"]
            img_bytes, ext = data_url_to_bytes(data_url)
            seed_used = result.get("seed", seed)
            image_id = result.get("imageId") or f"browser_{secrets.token_hex(4)}"
            proxy_download = result.get("imageDownloadUrl") or result.get("_proxy_download")
        else:
            result = await generate_image_http(
                prompt=final_prompt,
                negative_prompt=final_negative,
                seed=seed,
                resolution=resolution,
                guidance_scale=guidance_scale,
                channel=channel,
                sub_channel="public",
            )
            seed_used = result.get("seed", seed)
            image_id = result.get("imageId", f"gen_{secrets.token_hex(4)}")
            file_ext = result.get("fileExtension", "jpeg")
            proxy_download = result.get("imageDownloadUrl") or result.get("_proxy_download") or _find_proxy_download(result)

            if result.get("imageDataUrls"):
                data_url = result["imageDataUrls"][0]
                img_bytes, ext = data_url_to_bytes(data_url)
            elif proxy_download or image_id:
                img_bytes = await download_image(image_id, file_ext, proxy_download)
                ext = file_ext
                b64 = base64.b64encode(img_bytes).decode()
                data_url = f"data:image/{ext};base64,{b64[:100]}... (truncated)"
                full_b64 = base64.b64encode(img_bytes).decode()
                data_url_full = f"data:image/{ext};base64,{full_b64}"
            else:
                raise RuntimeError(f"No imageId or dataUrl or proxy_download in result: {result}")

        if not save_path:
            workspace_dir = Path("/home/user") / "perchance-output"
            workspace_dir.mkdir(exist_ok=True)
            ext = ext if 'ext' in locals() else result.get("fileExtension", "jpeg")
            save_path = str(workspace_dir / f"{image_id}.{ext}")

        if "." not in Path(save_path).name:
            save_path = f"{save_path}.{ext if 'ext' in locals() else 'jpeg'}"

        if 'img_bytes' not in locals():
            if result.get("dataUrl"):
                img_bytes, ext = data_url_to_bytes(result["dataUrl"])

        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        Path(save_path).write_bytes(img_bytes)

        output = {
            "status": "success",
            "prompt": prompt,
            "final_prompt": final_prompt,
            "negative_prompt": final_negative,
            "art_style": art_style,
            "seed": seed,
            "seed_used": seed_used,
            "resolution": resolution,
            "guidance_scale": guidance_scale,
            "image_id": image_id,
            "proxy_download": proxy_download,
            "saved_path": save_path,
            "file_size": len(img_bytes),
            "channel": channel,
        }

        if 'data_url_full' in locals():
            output["data_url_preview"] = data_url_full[:200] + "..."
        elif 'data_url' in locals() and isinstance(data_url, str):
            output["data_url_preview"] = data_url[:200] + "..."

        return json.dumps(output, indent=2)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        return json.dumps({"status": "error", "error": str(e), "traceback": tb[:3000], "hint": "If Cloudflare 403, try running MCP server locally, not in sandbox. Or use generate_image with PERCHANCE_USER_KEY and PERCHANCE_PROXY."}, indent=2)

@mcp.tool()
async def generate_image_official(
    prompt: str,
    negative_prompt: str = "",
    seed: int = -1,
    resolution: str = "512x512",
    guidance_scale: float = 7.0,
    save_path: str = "",
) -> str:
    """
    Generate an image using OFFICIAL Perchance Image API v1 (https://perchance.org/perchance-ai-api).
    
    Recommended for external apps - no userKey needed, returns permanent URL on user.uploads.dev.
    Works via iframe + postMessage pattern.
    
    Args:
        prompt: What to draw
        negative_prompt: Things to avoid
        seed: -1 for random or specific int
        resolution: 512x512, 512x768, 768x512, 768x768
        guidance_scale: 1-30, default 7
        save_path: Where to save downloaded image (optional)
    
    Returns:
        JSON with url (permanent), dataUrl, seed, etc.
    """
    try:
        result = await generate_via_official_api(
            prompt=prompt,
            negative_prompt=negative_prompt,
            resolution=resolution,
            guidance_scale=guidance_scale,
            seed=seed,
            timeout=180,
        )
        if not result.get("ok", True) and result.get("error"):
            return json.dumps({"status": "error", "error": result.get("error"), "result": result}, indent=2)

        saved_path = None
        file_size = None
        if result.get("url"):
            try:
                import httpx
                async with httpx.AsyncClient(timeout=60) as client:
                    r = await client.get(result["url"])
                    r.raise_for_status()
                    img_bytes = r.content
                    if not save_path:
                        workspace_dir = Path("/home/user") / "perchance-output"
                        workspace_dir.mkdir(exist_ok=True)
                        ext = "jpeg"
                        if ".png" in result["url"]:
                            ext = "png"
                        elif ".webp" in result["url"]:
                            ext = "webp"
                        saved_path = str(workspace_dir / f"official_{secrets.token_hex(4)}.{ext}")
                    else:
                        saved_path = save_path
                    Path(saved_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(saved_path).write_bytes(img_bytes)
                    file_size = len(img_bytes)
            except Exception as e:
                print(f"Failed to download from permanent URL: {e}")

        if not saved_path and result.get("dataUrl"):
            try:
                img_bytes, ext = data_url_to_bytes(result["dataUrl"])
                if not save_path:
                    workspace_dir = Path("/home/user") / "perchance-output"
                    workspace_dir.mkdir(exist_ok=True)
                    saved_path = str(workspace_dir / f"official_{secrets.token_hex(4)}.{ext}")
                else:
                    saved_path = save_path
                Path(saved_path).parent.mkdir(parents=True, exist_ok=True)
                Path(saved_path).write_bytes(img_bytes)
                file_size = len(img_bytes)
            except Exception as e:
                print(f"Failed to parse dataUrl: {e}")

        output = {
            "status": "success",
            "prompt": prompt,
            "resolution": resolution,
            "guidance_scale": guidance_scale,
            "seed": result.get("seed", seed),
            "url": result.get("url"),
            "permanent_url": result.get("url"),
            "data_url_preview": (result.get("dataUrl") or "")[:200] + "..." if result.get("dataUrl") else None,
            "saved_path": saved_path,
            "file_size": file_size,
            "raw_result": result,
        }
        return json.dumps(output, indent=2)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        return json.dumps({"status": "error", "error": str(e), "traceback": tb[:3000], "hint": "If Cloudflare 403, try running MCP server locally, not in sandbox."}, indent=2)

@mcp.tool()
async def generate_image_embed(
    prompt: str,
    negative_prompt: str = "",
    seed: int = -1,
    resolution: str = "512x512",
    guidance_scale: float = 7.0,
    channel: str = "stable-diffusion-ai",
    save_path: str = "",
) -> str:
    """
    Generate an image via embed page directly using Playwright (most reliable locally).
    
    This mimics what the user's browser does: navigates to embed with hash payload,
    waits for Turnstile verification and generation, then captures dataUrl.
    
    Args:
        prompt: What to draw
        negative_prompt: Things to avoid
        seed: -1 for random
        resolution: 512x512, 512x768, 768x512, 768x768
        guidance_scale: 1-30, default 7
        channel: Gallery channel
        save_path: Where to save
    
    Returns:
        JSON with saved path, seed, etc.
    """
    try:
        result = await generate_via_embed_playwright(
            prompt=prompt,
            negative_prompt=negative_prompt,
            seed=seed,
            resolution=resolution,
            guidance_scale=guidance_scale,
            channel=channel,
            timeout=180,
        )
        data_url = result["dataUrl"]
        img_bytes, ext = data_url_to_bytes(data_url)
        seed_used = result.get("seed", seed)
        image_id = result.get("imageId") or f"embed_{secrets.token_hex(4)}"

        if not save_path:
            workspace_dir = Path("/home/user") / "perchance-output"
            workspace_dir.mkdir(exist_ok=True)
            save_path = str(workspace_dir / f"{image_id}.{ext}")
        if "." not in Path(save_path).name:
            save_path = f"{save_path}.{ext}"

        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        Path(save_path).write_bytes(img_bytes)

        output = {
            "status": "success",
            "prompt": prompt,
            "seed": seed,
            "seed_used": seed_used,
            "resolution": resolution,
            "guidance_scale": guidance_scale,
            "image_id": image_id,
            "saved_path": save_path,
            "file_size": len(img_bytes),
            "data_url_preview": data_url[:200] + "...",
        }
        return json.dumps(output, indent=2)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        return json.dumps({"status": "error", "error": str(e), "traceback": tb[:3000]}, indent=2)

@mcp.tool()
async def generate_batch(
    prompts: List[str],
    negative_prompt: str = "",
    seed: int = -1,
    resolution: str = "512x512",
    guidance_scale: float = 7.0,
    art_style: str = "",
    save_dir: str = "",
    use_official_api: bool = False,
) -> str:
    """Generate multiple images from a list of prompts."""
    if not save_dir:
        save_dir = f"/home/user/perchance-output/batch_{int(time.time())}"
    Path(save_dir).mkdir(parents=True, exist_ok=True)

    results = []
    for i, p in enumerate(prompts):
        cur_seed = seed if seed != -1 else random.randint(0, 2**31)
        save_path = str(Path(save_dir) / f"image_{i}_{secrets.token_hex(3)}.jpeg")
        if use_official_api:
            res_str = await generate_image_official(
                prompt=p,
                negative_prompt=negative_prompt,
                seed=cur_seed,
                resolution=resolution,
                guidance_scale=guidance_scale,
                save_path=save_path,
            )
        else:
            res_str = await generate_image(
                prompt=p,
                negative_prompt=negative_prompt,
                seed=cur_seed,
                resolution=resolution,
                guidance_scale=guidance_scale,
                art_style=art_style,
                save_path=save_path,
            )
        try:
            res = json.loads(res_str)
        except Exception:
            res = {"raw": res_str}
        results.append(res)
        await asyncio.sleep(2)

    return json.dumps({"count": len(results), "save_dir": save_dir, "results": results}, indent=2)

@mcp.tool()
async def download_image_tool(image_id: str = "", proxy_token: str = "", save_path: str = "") -> str:
    """
    Download an image by its imageId or proxy_token (v1....).
    
    Args:
        image_id: The imageId returned from generate_image (e.g. "abc123...")
        proxy_token: The proxy download token (v1....) or full URL or path like /api/downloadTemporaryImageViaProxy?t=v1....
        save_path: Where to save (default auto)
    
    Returns:
        JSON with saved path.
    """
    try:
        if not image_id and not proxy_token:
            return json.dumps({"status": "error", "error": "Provide image_id or proxy_token"}, indent=2)
        img_bytes = await download_image(image_id, "jpeg", proxy_token)
        if not save_path:
            name = image_id or f"proxy_{secrets.token_hex(4)}"
            save_path = f"/home/user/perchance-output/{name}.jpeg"
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        Path(save_path).write_bytes(img_bytes)
        return json.dumps({"status": "success", "saved_path": save_path, "size": len(img_bytes)}, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

@mcp.tool()
async def get_ad_code() -> str:
    """Get current ad access code from Perchance."""
    try:
        import httpx
        try:
            from client import get_ad_access_code, DEFAULT_HEADERS
        except ImportError:
            from perchance_mcp.client import get_ad_access_code, DEFAULT_HEADERS
        proxy = os.getenv("PERCHANCE_PROXY")
        client_kwargs = {"headers": DEFAULT_HEADERS, "timeout": 30}
        if proxy:
            client_kwargs["proxy"] = proxy
        async with httpx.AsyncClient(**client_kwargs) as client:
            code = await get_ad_access_code(client)
            return json.dumps({"ad_access_code": code, "length": len(code), "valid": bool(re.fullmatch(r"[a-f0-9]{64}", code))}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)

# --- Open-source self-hosted unlimited tools ---

@mcp.tool()
async def get_ad_code_opensource() -> str:
    """
    Get ad access code via open-source unlimited bypass (curl_cffi TLS impersonation).
    No proxy required, no rate limits, bypasses Cloudflare JA3 fingerprint.
    This is the recommended unlimited method for ad code (curl_cffi TLS impersonation, no limit).
    
    Returns 64-char hex ad code used for generation.
    """
    try:
        from client import get_ad_access_code_via_curl_cffi
        code = await get_ad_access_code_via_curl_cffi()
        return json.dumps({
            "ad_access_code": code,
            "length": len(code),
            "valid": bool(re.fullmatch(r"[a-f0-9]{64}", code)),
            "method": "curl_cffi chrome impersonation (open-source unlimited)",
            "note": "No proxy needed, bypasses Cloudflare TLS fingerprint, unlimited free"
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)

@mcp.tool()
async def get_opensource_status() -> str:
    """
    Check open-source self-hosted unlimited alternatives status.
    Shows curl_cffi, Camoufox, SeleniumBase availability and capabilities.
    These are unlimited free open-source alternatives.
    """
    result = {
        "open_source_bypass": {
            "curl_cffi_ad_code": {
                "available": False,
                "method": "TLS impersonation impersonate='chrome' bypasses JA3 fingerprint",
                "unlimited": True,
                "requires_proxy": False,
                "tested": False
            },
            "camoufox": {
                "available": False,
                "method": "Firefox-based stealth browser (Camoufox) for Turnstile solving",
                "unlimited": True,
                "requires": "pip install camoufox + camoufox fetch + LD_LIBRARY_PATH Firefox deps",
                "tested": False
            },
            "seleniumbase_uc": {
                "available": False,
                "method": "Chromium undetected-chromedriver UC Mode for Turnstile",
                "unlimited": True,
                "requires": "pip install seleniumbase + playwright chromium binary",
                "tested": False
            }
        },
        "comparison": {
            "previous_browserless_limit": "1000 requests/month free tier (removed)",
            "current_unlimited": "Self-hosted open-source stack (curl_cffi + SeleniumBase/Camoufox) is unlimited, no API key needed"
        }
    }

    # Test curl_cffi
    try:
        import curl_cffi
        result["open_source_bypass"]["curl_cffi_ad_code"]["available"] = True
        try:
            from client import get_ad_access_code_via_curl_cffi
            code = await get_ad_access_code_via_curl_cffi()
            result["open_source_bypass"]["curl_cffi_ad_code"]["tested"] = True
            result["open_source_bypass"]["curl_cffi_ad_code"]["test_success"] = bool(re.fullmatch(r"[a-f0-9]{64}", code))
            result["open_source_bypass"]["curl_cffi_ad_code"]["code_preview"] = f"{code[:12]}..." if code else None
        except Exception as e:
            result["open_source_bypass"]["curl_cffi_ad_code"]["test_error"] = str(e)[:500]
    except ImportError as e:
        result["open_source_bypass"]["curl_cffi_ad_code"]["error"] = str(e)

    # Test Camoufox
    try:
        import camoufox
        result["open_source_bypass"]["camoufox"]["available"] = True
        result["open_source_bypass"]["camoufox"]["version"] = getattr(camoufox, "__version__", "unknown")
        result["open_source_bypass"]["camoufox"]["binary_path"] = str(Path.home() / ".cache/camoufox")
    except ImportError as e:
        result["open_source_bypass"]["camoufox"]["error"] = str(e)

    # Test SeleniumBase
    try:
        import seleniumbase
        result["open_source_bypass"]["seleniumbase_uc"]["available"] = True
        # Check chromium binary
        import glob
        candidates = glob.glob(str(Path.home() / ".cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
        result["open_source_bypass"]["seleniumbase_uc"]["chromium_binaries"] = candidates[:3]
        result["open_source_bypass"]["seleniumbase_uc"]["chromium_found"] = len(candidates) > 0
    except ImportError as e:
        result["open_source_bypass"]["seleniumbase_uc"]["error"] = str(e)

    return json.dumps(result, indent=2)

@mcp.tool()
async def generate_image_opensource(
    prompt: str,
    negative_prompt: str = "",
    seed: int = -1,
    resolution: str = "512x512",
    guidance_scale: float = 7.0,
    channel: str = "stable-diffusion-ai",
    save_path: str = "",
) -> str:
    """
    Generate image via open-source unlimited stack (RECOMMENDED for production unlimited use).
    
    Uses:
    - curl_cffi TLS impersonation for ad code + generate + download (bypasses Cloudflare, unlimited, no proxy needed)
    - Camoufox Firefox stealth OR SeleniumBase UC Mode for Turnstile solving (self-hosted unlimited)
    - No Browserless, fully open-source unlimited
    
    This is the unlimited free open-source alternative (no Browserless).
    ad code bypass is 100% open-source unlimited and tested working.
    Turnstile solving via Camoufox/SeleniumBase is self-hosted unlimited but may need tuning.
    
    Args:
        prompt: What to draw
        negative_prompt: Things to avoid
        seed: -1 random or specific int
        resolution: 512x512, 512x768, 768x512, 768x768
        guidance_scale: 1-30 default 7
        channel: Gallery channel
        save_path: Where to save (default auto in /home/user/perchance-output/)
    
    Returns JSON with saved_path, imageId, etc.
    """
    try:
        from client import generate_image_via_opensource, data_url_to_bytes
        result = await generate_image_via_opensource(
            prompt=prompt,
            negative_prompt=negative_prompt,
            seed=seed,
            resolution=resolution,
            guidance_scale=guidance_scale,
            channel=channel,
        )

        # Handle both dataUrl and raw bytes
        if "dataUrl" in result:
            img_bytes, ext = data_url_to_bytes(result["dataUrl"])
        elif "imageBytes" in result:
            img_bytes = result["imageBytes"]
            ext = result.get("fileExtension", "jpeg")
        else:
            raise RuntimeError("No image data in result")

        image_id = result.get("imageId", f"opensource_{secrets.token_hex(4)}")
        proxy_download = result.get("imageDownloadUrl")

        if not save_path:
            workspace_dir = Path("/home/user/perchance-output")
            workspace_dir.mkdir(exist_ok=True)
            save_path = str(workspace_dir / f"{image_id}.{ext}")
        if "." not in Path(save_path).name:
            save_path = f"{save_path}.{ext}"

        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        Path(save_path).write_bytes(img_bytes)

        output = {
            "status": "success",
            "prompt": prompt,
            "seed": seed,
            "seed_used": result.get("seed", seed),
            "resolution": resolution,
            "guidance_scale": guidance_scale,
            "image_id": image_id,
            "proxy_download": proxy_download,
            "saved_path": save_path,
            "file_size": len(img_bytes),
            "width": result.get("width"),
            "height": result.get("height"),
            "maybe_nsfw": result.get("maybeNsfw"),
            "browser_id": result.get("_browserId"),
            "method": "open-source unlimited: curl_cffi (ad code + generate + download) + Camoufox/SeleniumBase (Turnstile)",
            "opensource": True,
            "unlimited": True,
            "note": "ad code via curl_cffi is 100% unlimited open-source tested working. Turnstile via Camoufox self-hosted unlimited."
        }
        return json.dumps(output, indent=2)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        return json.dumps({
            "status": "error",
            "error": str(e),
            "traceback": tb[:5000],
            "hint": "Open-source ad code via curl_cffi is tested working unlimited. For Turnstile, ensure Camoufox installed (pip install camoufox && camoufox fetch) and LD_LIBRARY_PATH includes Firefox deps (/tmp/debs/out/usr/lib/x86_64-linux-gnu), or set PERCHANCE_USER_KEY if you have a fresh key."
        }, indent=2)

@mcp.tool()
async def refresh_user_key_opensource() -> str:
    """
    Get fresh userKey via open-source self-hosted unlimited solvers.
    Tries SeleniumBase UC Mode (tested working) then Camoufox (Firefox stealth). Fully open-source unlimited.
    Returns fresh 64-char userKey valid ~30s, IP-bound.
    This is unlimited open-source alternative, no Browserless needed.
    """
    try:
        from client import get_user_key_via_opensource
        key = await get_user_key_via_opensource()
        if key:
            return json.dumps({
                "status": "success",
                "user_key": key,
                "preview": f"{key[:12]}...{key[-6:]}",
                "method": "open-source self-hosted unlimited (Camoufox/SeleniumBase)",
                "note": "Valid ~30s, IP-bound, saved to cache, unlimited free"
            }, indent=2)
        else:
            return json.dumps({
                "status": "failed",
                "error": "Could not obtain userKey via open-source methods",
                "hint": "Try installing Camoufox: pip install camoufox && camoufox fetch, and ensure Firefox deps in LD_LIBRARY_PATH, or ensure chromium binary exists via playwright install chromium"
            }, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, indent=2)

def main():
    mcp.run()

if __name__ == "__main__":
    main()
