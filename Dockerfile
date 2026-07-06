
# Debian-basiertes Image für bessere Kompatibilität mit Chromium/patchright
FROM python:3.13-slim

WORKDIR /app

# System dependencies + unprivileged user in one layer
RUN apt-get update && apt-get install -y \
    ffmpeg \
    mpv \
    xvfb \
    xauth \
    x11-utils \
    ca-certificates \
    dbus \
    dbus-x11 \
    locales \
    tzdata \
    fonts-liberation \
    fonts-dejavu-core \
    fonts-noto-core \
    fonts-noto-color-emoji \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libc6 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libexpat1 \
    libgbm1 \
    libgcc-s1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    xdg-utils \
    --no-install-recommends && \
    rm -rf /var/lib/apt/lists/* && \
    sed -i 's/^# *\(de_DE.UTF-8\)/\1/' /etc/locale.gen && locale-gen && \
    ln -fs /usr/share/zoneinfo/Europe/Berlin /etc/localtime && \
    mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix && \
    useradd -m -d /home/mediaforge mediaforge && \
    mkdir -p /app/Downloads /home/mediaforge/.mediaforge /home/mediaforge/.aniworld && \
    chown -R mediaforge:mediaforge /app /home/mediaforge

# .aniworld is only the mount point for legacy "AniWorld Downloader" volumes.
# Pre-creating it with the right ownership means users migrating from the old
# image can just add "- aniworld-data:/home/mediaforge/.aniworld:ro" to their
# compose file — no manual chown needed. legacy_import.py picks it up and
# copies the data on first boot; the folder stays empty and harmless otherwise.

# Container-friendly Python defaults
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Default download directory
ENV MEDIAFORGE_DOWNLOAD_PATH=/app/Downloads \
    MEDIAFORGE_DOCKER=1

# Realistic locale / timezone so the captcha browser doesn't look like a bare
# UTC server (Turnstile evaluates these signals).
ENV TZ=Europe/Berlin \
    LANG=de_DE.UTF-8 \
    LANGUAGE=de_DE:de \
    LC_ALL=de_DE.UTF-8

# Install patchright browsers to a global path accessible by the unprivileged runtime user.
# This step is intentionally placed BEFORE copying source code so that the heavy
# Chromium download is cached independently and only re-runs when pyproject.toml changes.
#
# The version installed here MUST match the "patchright==" pin in pyproject.toml
# exactly (not just "patchright", unpinned) — otherwise `pip install .` below can
# resolve a different patchright/playwright version against the full dependency
# graph than the standalone install above, leaving the downloaded Chromium
# revision (baked into this layer) mismatched against what actually runs at
# startup. That mismatch is exactly what triggers Patchright's
# "Looks like Playwright was just installed or updated — run patchright install"
# warning at container start. Bump both places together when upgrading.
COPY pyproject.toml README.md LICENSE MANIFEST.in /app/
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir "patchright==1.61.2" && \
    patchright install chromium && \
    chmod -R 755 /opt/ms-playwright

# Copy source and install the full project
COPY --chown=mediaforge:mediaforge src/ /app/src/
RUN pip install --no-cache-dir .

# Entrypoint script for logged startup sequence
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Drop privileges for runtime
USER mediaforge

# Expose the web UI port
EXPOSE 8080

# Start with a virtual X server; poll until it's ready before launching the app
ENV DISPLAY=:99

# Health check: verify the web UI is reachable
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3     CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/')" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
