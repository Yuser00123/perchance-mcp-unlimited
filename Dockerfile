# Perchance MCP - Fully Open-Source Unlimited (No Browserless)
FROM python:3.11-slim

# Install system deps for Chromium + Firefox (Camoufox) + curl
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    gnupg \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libatspi2.0-0 \
    libasound2 \
    libcups2 \
    libdrm2 \
    libxdamage1 \
    libxkbcommon0 \
    libxcomposite1 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libgtk-3-0 \
    libxfixes3 \
    libxext6 \
    libx11-6 \
    libglib2.0-0 \
    libgdk-pixbuf-2.0-0 \
    libcairo2 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libgraphene-1.0-0 \
    libepoxy0 \
    libx11-xcb1 \
    libxcb1 \
    libxcb-shm0 \
    libxcb-dri2-0 \
    libxcb-dri3-0 \
    libxcb-glx0 \
    libxcb-present0 \
    libxcb-sync1 \
    libxcb-xfixes0 \
    libxtst6 \
    libxshmfence1 \
    libwayland-client0 \
    libwayland-cursor0 \
    libwayland-egl1 \
    libcloudproviders0 \
    libcolord2 \
    libdconf1 \
    libpolkit-gobject-1-0 \
    fonts-liberation \
    libappindicator3-1 \
    xdg-utils \
    xvfb \
    --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright chromium
RUN playwright install chromium
RUN playwright install-deps chromium

# Install Camoufox browser (optional, for Turnstile)
RUN pip install camoufox && camoufox fetch || echo "Camoufox fetch failed, continuing"

# Copy app
COPY . .

# Create output dir
RUN mkdir -p /app/perchance-output /home/user/perchance-output /tmp/debs/out/usr/lib/x86_64-linux-gnu

ENV PYTHONUNBUFFERED=1
ENV LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH

# Expose port for remote MCP (SSE)
EXPOSE 8000

# For stdio MCP (default)
# CMD ["python", "server.py"]

# For remote MCP (SSE) - use remote_server.py
CMD ["python", "remote_server.py"]
