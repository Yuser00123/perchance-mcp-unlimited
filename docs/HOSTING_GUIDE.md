# Hosting Perchance MCP Remotely (Open-Source Unlimited, No Browserless)

Your MCP server is now **100% open-source unlimited** (15 tools, no Browserless API keys). You can host it remotely for agents to use via SSE.

## Current Mode

- `server.py` → **stdio** (for Claude Desktop, Cursor, local agents)
- `remote_server.py` → **SSE** (for remote hosting: Fly.io, Render, Railway, Cloud Run)

Both expose same 15 tools, including `generate_image_opensource` which is tested working (cosmic fear garou 147k JPEG).

---

## Option 1: Smithery.ai (Easiest for MCP Registry)

Smithery hosts MCP servers and handles transport.

```bash
# Install Smithery CLI
npm install -g @smithery/cli

# Publish
cd /home/user/perchance-mcp
smithery publish --config mcp_config.json
```

Or add to Smithery registry via GitHub:
1. Push your repo to GitHub
2. Go to https://smithery.ai and "Add Server"
3. Point to your repo with `server.py` or `remote_server.py`
4. Smithery auto-detects FastMCP and hosts with SSE

**Pros:** No infra management, auto-scaling, MCP-specific
**Cons:** May need paid plan for heavy image generation

---

## Option 2: Fly.io (Recommended for Chrome + Unlimited)

Fly.io supports Docker with Chrome deps and is cheap for this workload.

```bash
# Install flyctl
curl -L https://fly.io/install.sh | sh

# Launch
cd /home/user/perchance-mcp
fly launch --dockerfile Dockerfile --name perchance-mcp

# Set env (no API keys needed!)
fly secrets set LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu

# Deploy
fly deploy

# Your remote MCP URL:
# https://perchance-mcp.fly.dev/sse
```

**fly.toml** (auto-generated, but ensure):
```toml
app = "perchance-mcp"
primary_region = "sfo"

[build]
  dockerfile = "Dockerfile"

[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = true
  auto_start_machines = true
  min_machines_running = 0

[[services]]
  internal_port = 8000
  protocol = "tcp"
  auto_stop_machines = true
  auto_start_machines = true

  [[services.ports]]
    port = 443
    handlers = ["http", "tls"]
```

**Cost:** ~$5-10/month for always-on, or $0 with auto-stop.

---

## Option 3: Render.com (Free Tier Available)

1. Go to https://render.com → New → Web Service
2. Connect GitHub repo
3. Settings:
   - **Runtime:** Docker
   - **Dockerfile Path:** `./Dockerfile`
   - **Port:** 8000
   - **Env:** `LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu`
4. Deploy → URL: `https://perchance-mcp.onrender.com/sse`

**Pros:** Free tier, easy
**Cons:** Free tier spins down, cold start ~30s (SeleniumBase needs Chrome)

---

## Option 4: Railway.app

```bash
npm i -g @railway/cli
railway login
cd /home/user/perchance-mcp
railway init
railway up
# Set env in dashboard: LD_LIBRARY_PATH
# URL: https://perchance-mcp.up.railway.app/sse
```

---

## Option 5: Google Cloud Run (Serverless, Pay-per-use)

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT

# Build and push
gcloud builds submit --tag gcr.io/YOUR_PROJECT/perchance-mcp

# Deploy
gcloud run deploy perchance-mcp \
  --image gcr.io/YOUR_PROJECT/perchance-mcp \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --port 8000 \
  --memory 2Gi \
  --timeout 300 \
  --set-env-vars LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu

# URL: https://perchance-mcp-xyz-uc.a.run.app/sse
```

**Pros:** Scales to zero, pay only when generating
**Cons:** Needs 2GB RAM for Chrome, cold start

---

## Option 6: Self-Hosted VPS (Hetzner, DigitalOcean, EC2)

Cheapest for unlimited.

```bash
# On VPS (Ubuntu 22.04)
sudo apt update && sudo apt install -y docker.io
git clone YOUR_REPO
cd perchance-mcp

docker build -t perchance-mcp .
docker run -d -p 8000:8000 --name perchance-mcp \
  -e LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu \
  perchance-mcp

# With Caddy/Nginx for HTTPS:
# https://your-vps-ip/sse → proxy to localhost:8000
```

**Cost:** Hetzner CX11 €3.79/month, unlimited generation.

---

## Option 7: Hugging Face Spaces (Free, Docker)

1. Create Space at https://huggingface.co/new-space
2. Choose Docker SDK
3. Push Dockerfile + code
4. Set port 7860 (HF default) → update remote_server.py to use PORT env
5. URL: `https://your-username-perchance-mcp.hf.space/sse`

---

## How Remote MCP Clients Connect

### For Claude Desktop (remote SSE)

Edit `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "perchance-remote": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://perchance-mcp.fly.dev/sse"]
    }
  }
}
```

Or if your host supports Streamable HTTP:

```json
{
  "mcpServers": {
    "perchance-remote": {
      "url": "https://perchance-mcp.fly.dev/sse",
      "transport": "sse"
    }
  }
}
```

### For Cursor / Windsurf

Same config, or use `mcp-remote` package.

### For Custom Agent (Python)

```python
from mcp import ClientSession
from mcp.client.sse import sse_client

async with sse_client("https://perchance-mcp.fly.dev/sse") as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool("generate_image_opensource", {
            "prompt": "cosmic fear garou, galaxy eyes",
            "resolution": "768x768"
        })
```

---

## Important: Deps for Hosting

Your MCP needs:
- **Chromium binary**: `playwright install chromium` (Dockerfile does this)
- **System libs**: libnss3, libgtk-3-0, libgbm1, etc. (Dockerfile installs all)
- **curl_cffi**: For Cloudflare bypass (no extra deps)
- **SeleniumBase**: For Turnstile solving (needs Chrome, tested working)
- **Camoufox**: Optional, needs Firefox deps (Dockerfile includes)

**Without these, Turnstile solving fails with 600010.**

The provided `Dockerfile` installs everything and is tested in E2B sandbox with `LD_LIBRARY_PATH=/tmp/debs/out/usr/lib/x86_64-linux-gnu`.

For non-Docker hosts (Render, Railway), ensure they run `playwright install chromium` and `playwright install-deps`.

---

## Recommended: Fly.io

For your use case (Prayagraj, IN, need unlimited, no Browserless):

**Fly.io is best** because:
- Supports Docker with Chrome (unlike Vercel)
- Cheap, auto-stop saves cost
- Region `sin` (Singapore) close to India for low latency to Perchance
- 2GB RAM enough for SeleniumBase UC Mode
- No API keys needed (fully open-source)

```bash
fly launch --region sin --dockerfile Dockerfile
# URL: https://perchance-mcp.fly.dev/sse
# Use this URL in your remote MCP clients
```

---

## Security

- No secrets needed! Fully open-source unlimited.
- If you add `PERCHANCE_USER_KEY` for caching, set as secret: `fly secrets set PERCHANCE_USER_KEY=...`
- Rate limit your remote endpoint if public (e.g., Cloudflare in front)

---

## Test Remote Deployment

After deploying, test:

```bash
curl https://perchance-mcp.fly.dev/sse
# Should return SSE stream

# Or via MCP Inspector
npx @modelcontextprotocol/inspector
# Enter URL: https://perchance-mcp.fly.dev/sse
# Call generate_image_opensource with prompt "test"
```

---

## Summary

| Host | Cost | Chrome Support | Cold Start | Best For |
|------|------|----------------|------------|----------|
| **Fly.io** | $5/mo | ✅ Docker | Fast | **Recommended for you** |
| Render | Free-$7 | ✅ Docker | 30s | Free tier |
| Railway | $5/mo | ✅ Docker | Fast | Easy |
| Cloud Run | Pay-per-use | ✅ Docker | 10s | Serverless |
| Smithery | Free-$10 | ✅ | Fast | MCP registry |
| VPS Hetzner | €3.79/mo | ✅ | None | Cheapest unlimited |
| HF Spaces | Free | ✅ Docker | 20s | Free |

**Your next step:**
```bash
cd /home/user/perchance-mcp
fly launch --region sin
# Share the https://.../sse URL with your agents
```

All 15 tools will be available remotely, including `generate_image_opensource` that generated cosmic fear garou 147k JPEG proof.
