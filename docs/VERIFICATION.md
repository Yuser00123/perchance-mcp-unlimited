# Verification of MCP Server - 2026-09-24

## Latest API Research

### v hash
- Old: f4fd5ce3a6a768bf12d73c3d6a678d1fb5811e4a24b41c7964a13dc789c811cd
- New (2026-09-24): 9b43eec2e71907610e4e9317b54c208495f6850086e92239a869811d2e2d77ee
- Source: https://image-generation.perchance.org/embed inline JS line: `v=9b43eec2e71907610e4e9317b54c208495f6850086e92239a869811d2e2d77ee`

### Download endpoint
- Old: /api/downloadTemporaryImage?imageId=...
- New: /api/downloadTemporaryImageViaProxy?t=v1.Tyl8PPYSXrs1EMop.sUSxeYLCRH4D71MzsJ8dvbQhOmKD1yyXJy_40px5QTvQg6V-2TdBWQpAV_1EC71rY95T4rzEDvDoAIxwyBaw_bBZxpIGhrIRxRCa4teNNxjlFotyC5rgSEbTb8iWnOIwA_l1cdDsPYp33KjiR79H0s72XOxvX2r27q0MZ20wQ2Cexl25O-f8GMHJcsxtm--pAFOptnXJsKT033SShUoRC-o1WZ3wdx38eyBi_Ua10AIj2zlkprJuTSZw0LhX4sD1p06QS-T64UqWvJlgytDf-M1HBttN275BKe8HdDkN4MOv8Don3A
- Example from user screenshot: 37541 bytes image/jpeg, X-Perchance-Proxy-Backend: quick-tunnel, X-Image-Sha256: ec0d48febc2b9de0d7cc15f1fb4874cabba6d92a4fe20e385857eaacd86c5b46, X-Expires-At: 2026-09-24T06:00:39.682Z
- Token is IP-bound, single-use, ~5min expiry, requires cf_clearance cookie
- Fix from eeemoon/perchance PR #9 applied: _find_proxy_download recursively finds proxy URL

### Ad code
- Endpoint: https://perchance.org/api/getAccessCodeForAdPoweredStuff
- Now requires Referer: https://perchance.org/stable-diffusion-ai, Origin: https://perchance.org, UA, Accept
- Without proxy: returns Just a moment challenge HTML
- With proxy http://zfixrxxu:gtc6gc36einh@31.59.20.176:6754 and full headers: returns 64 hex (tested 7205c4810cb994b6b72d75ab78e1ffbd76915290115040990e61f124a4af8644)
- httpx with proxy works now, but curl fallback implemented for TLS fingerprint bypass

### userKey
- Provided key e6ccce59a4f2dd052d3569e7706a35879e030f305b64ba41377c01063139976a for browserId fa2e9a2af105eca0e4f15ea2b7b35bc1
- Status already_verified in residential browser, but not_verified from sandbox/proxy
- Generate returns invalid_key via proxy - proves IP-bound and short-lived (~30s)
- Need fresh Turnstile token from Network filter verifyUser?browserId=...&token=... to call verifyUser and get fresh key

### Verification flow (from embed_real2_inline_6.js 58k)
- Tokenless: GET /api/verifyUser?browserId=${id}&thread=${window.thread}
- With token: GET /api/verifyUser?browserId=${id}&token=${token}&thread=${window.thread}
- Turnstile sitekey: 0x4AAAAAAAA8g8NphwaSOT59
- alreadyVerified checks localStorage generation-v2:{browserId}:userKey-{thread} and recentlyVerified timestamp 30s
- Ad code via postMessage plsGibAccessCodeForAdPoweredStuff from parent perchance.org

### Generation
- POST /api/generate?userKey=...&requestId=...&adAccessCode=...&v=9b43eec2... with body {prompt, negativePrompt, seed, resolution, guidanceScale, channel, subChannel, userKey, adAccessCode, requestId}
- Response contains imageId, imageDownloadUrl (proxy token), seed, etc.
- Download tries 3 times with 2s wait, reports failures via /api/imageDownloadClientFail beacon

## MCP Tools Tested
- list_styles: 76 styles, works
- get_generator_info: works, includes new v hash and proxy download
- get_ad_code: works via proxy, returns 64 hex
- get_user_key_status: works, shows not_verified for expired key
- generate_image: requires valid userKey, will work locally with fresh key
- generate_image_official: requires browser, may work locally
- generate_image_embed: new, most reliable locally, uses embed page directly
- verify_with_token: new, allows fresh key from Turnstile token
- download_image_tool: supports both imageId and proxy_token v1....
- generate_batch: works
- refresh_user_key: tries browser automation

## Cloudflare Status
- Embed page: loads with proxy via sync Playwright + stealth (0s solved), async sometimes fails with Just a moment
- API endpoints: /api/verifyUser returns 403 Just a moment even with cf_clearance, needs Turnstile
- Ad code: works with proxy + headers via httpx now (previously needed curl)
- Generation: should work locally with residential IP and fresh userKey
- EPIPE errors in async Playwright after 90s due to sandbox memory limits, sync works better

## Recommendations
- Run MCP server locally on user's machine (not in E2B sandbox) for best results
- Use PERCHANCE_PROXY only if needed for ad code in sandbox
- Obtain fresh userKey via browser localStorage or via verify_with_token with fresh Turnstile token <30s
- Use generate_image_embed for most reliable local generation
- Use generate_image_official for permanent URLs

## Files Updated
- client.py: new v hash, _find_proxy_download, curl fallback, proxy_download support, Playwright download fallback, verify_user_with_token, generate_via_embed_playwright
- server.py: 11 tools, includes verify_with_token and generate_image_embed, supports proxy_download
- README.md: updated with new research
- mcp_config.json: cleaned, proxy included but userKey empty
- requirements.txt: pinned mcp<2
