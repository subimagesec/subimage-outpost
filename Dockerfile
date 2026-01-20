FROM python:3.11.14-slim@sha256:c24e9effa2821a6885165d930d939fec2af0dcf819276138f11dd45e200bd032

# Install system dependencies
RUN apt-get update && \
    apt-get install -y curl iproute2 iputils-ping gnupg && \
    apt-get clean

# Add Tailscale APT repo (securely)
RUN curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.gpg | gpg --dearmor -o /usr/share/keyrings/tailscale-archive-keyring.gpg && \
    curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.list | tee /etc/apt/sources.list.d/tailscale.list > /dev/null && \
    echo "deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/debian bookworm main" > /etc/apt/sources.list.d/tailscale.list && \
    apt-get update && \
    apt-get install -y tailscale && \
    apt-get clean

# Install Python dependencies
RUN pip install fastapi uvicorn httpx

# Copy proxy script
COPY proxy.py /proxy.py
COPY start.sh /start.sh
RUN chmod +x /start.sh

ENTRYPOINT ["/start.sh"]