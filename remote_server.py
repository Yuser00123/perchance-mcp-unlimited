"""
Remote MCP Server for Perchance - Hostable via SSE / Streamable HTTP
For remote hosting on Fly.io, Render, Railway, Cloud Run, etc.

Current server.py uses stdio (for Claude Desktop).
This file exposes same tools via SSE for remote agents.

Usage:
  python remote_server.py  # Runs on 0.0.0.0:8000 with SSE
  Then connect via: https://your-host/sse

Environment:
  - No API keys needed (fully open-source unlimited)
  - Optional: PERCHANCE_USER_KEY for cached key
  - LD_LIBRARY_PATH auto-set for sandbox deps
"""

import os
from pathlib import Path

def _setup_ld():
    for p in ["/tmp/debs/out/usr/lib/x86_64-linux-gnu", "/home/user/.perchance-deps/lib", "/usr/lib/x86_64-linux-gnu"]:
        if os.path.exists(p):
            cur = os.environ.get("LD_LIBRARY_PATH","")
            if p not in cur:
                os.environ["LD_LIBRARY_PATH"] = f"{p}:{cur}" if cur else p
_setup_ld()

# Import FastMCP from server.py (reuses same tools)
from server import mcp

# For mcp<2, FastMCP can run with different transports
# stdio is default for local, sse for remote

if __name__ == "__main__":
    # Get port from env (for Cloud Run, Render, etc.)
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")

    print(f"Starting Perchance Remote MCP Server (Open-Source Unlimited, No Browserless)")
    print(f"  Host: {host}:{port}")
    print(f"  Transport: sse")
    print(f"  Tools: {len(mcp._tool_manager._tools)}")
    print(f"  Stack: curl_cffi (ad code) + SeleniumBase UC (Turnstile) - unlimited free")
    print(f"  Endpoints:")
    print(f"    - SSE: http://{host}:{port}/sse")
    print(f"    - For remote MCP clients, use URL: https://your-domain/sse")

    # Run with SSE transport for remote hosting
    # For mcp<2: FastMCP.run(transport='sse', host, port)
    try:
        # Try new API (mcp<2)
        mcp.run(transport="sse", host=host, port=port)
    except TypeError:
        # Fallback for older FastMCP
        try:
            mcp.run(host=host, port=port)
        except Exception as e:
            print(f"Failed to run with sse, trying stdio fallback: {e}")
            mcp.run()
