#!/usr/bin/env python3
"""
CLI for Perchance image generation - useful for testing without MCP
Supports both internal and official APIs
"""
import asyncio
import argparse
import json
import os
import sys
from pathlib import Path

# Setup LD path
for p in ["/tmp/debs/out/usr/lib/x86_64-linux-gnu", f"{Path.home()}/.perchance-deps/lib"]:
    if os.path.exists(p):
        cur = os.environ.get("LD_LIBRARY_PATH","")
        if p not in cur:
            os.environ["LD_LIBRARY_PATH"] = f"{p}:{cur}" if cur else p

from client import (
    generate_image_http,
    generate_via_official_api,
    download_image,
    load_styles,
    apply_style,
    data_url_to_bytes,
)

async def main():
    parser = argparse.ArgumentParser(description="Perchance Stable Diffusion CLI")
    parser.add_argument("prompt", help="Prompt to generate")
    parser.add_argument("--negative", default="", help="Negative prompt")
    parser.add_argument("--seed", type=int, default=-1, help="Seed (-1 for random)")
    parser.add_argument("--resolution", default="512x512", help="Resolution: 512x512, 512x768, 768x512, 768x768 or portrait/square/landscape")
    parser.add_argument("--guidance", type=float, default=7.0, help="Guidance scale 1-30")
    parser.add_argument("--style", default="", help="Art style name")
    parser.add_argument("--output", default="", help="Output path")
    parser.add_argument("--official", action="store_true", help="Use official API v1 (perchance-ai-api) instead of internal")
    parser.add_argument("--list-styles", action="store_true", help="List styles and exit")
    args = parser.parse_args()

    if args.list_styles:
        styles = load_styles()
        print(f"Available styles ({len(styles)}):")
        for name in sorted(styles.keys()):
            print(f"  - {name}")
        return

    final_prompt = args.prompt
    if args.style:
        styles = load_styles()
        final_prompt = apply_style(args.prompt, args.style, styles)
        print(f"Styled prompt: {final_prompt[:300]}")

    print(f"Generating: {final_prompt[:100]}... via {'official' if args.official else 'internal'} API")

    if args.official:
        result = await generate_via_official_api(
            prompt=final_prompt,
            negative_prompt=args.negative,
            seed=args.seed,
            resolution=args.resolution,
            guidance_scale=args.guidance,
        )
        print(f"Result: {json.dumps(result, indent=2)[:2000]}")
        if result.get("url"):
            print(f"Permanent URL: {result['url']}")
            # Try download
            import httpx
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.get(result["url"])
                r.raise_for_status()
                out = args.output or f"/home/user/perchance-output/official_{result.get('seed','unknown')}.jpeg"
                Path(out).parent.mkdir(parents=True, exist_ok=True)
                Path(out).write_bytes(r.content)
                print(f"Saved to {out} ({len(r.content)} bytes)")
        elif result.get("dataUrl"):
            img_bytes, ext = data_url_to_bytes(result["dataUrl"])
            out = args.output or f"/home/user/perchance-output/official_{result.get('seed','unknown')}.{ext}"
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(img_bytes)
            print(f"Saved to {out} from dataUrl")
    else:
        result = await generate_image_http(
            prompt=final_prompt,
            negative_prompt=args.negative,
            seed=args.seed,
            resolution=args.resolution,
            guidance_scale=args.guidance,
        )
        print(f"Result: {json.dumps(result, indent=2)[:1000]}")

        image_id = result.get("imageId")
        if not image_id and result.get("imageDataUrls"):
            data_url = result["imageDataUrls"][0]
            header, b64 = data_url.split(",", 1)
            import base64
            data = base64.b64decode(b64)
            out = args.output or f"/home/user/perchance-output/{result.get('seed', 'unknown')}.jpeg"
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(data)
            print(f"Saved to {out}")
        elif image_id:
            data = await download_image(image_id)
            out = args.output or f"/home/user/perchance-output/{image_id}.jpeg"
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(data)
            print(f"Saved to {out} ({len(data)} bytes) seed={result.get('seed')}")
        else:
            print("No imageId or dataUrl in result")

if __name__ == "__main__":
    asyncio.run(main())
