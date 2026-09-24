#!/bin/bash
# Run Perchance MCP Server
# Handles LD_LIBRARY_PATH for sandboxed environments where Playwright deps are manually installed
cd "$(dirname "$0")"
export PYTHONPATH="$(pwd):$PYTHONPATH"
# Fix for missing chromium libs in sandbox
if [ -d "/tmp/debs/out/usr/lib/x86_64-linux-gnu" ]; then
  export LD_LIBRARY_PATH="/tmp/debs/out/usr/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH"
fi
if [ -d "$HOME/.perchance-deps/lib" ]; then
  export LD_LIBRARY_PATH="$HOME/.perchance-deps/lib:$LD_LIBRARY_PATH"
fi
python server.py
