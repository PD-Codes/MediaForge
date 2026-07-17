
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
    libgl1-mesa-dri \
    libglx-mesa0 \
    libxkbcommon0 \
    libatspi2.0-0 \
    --no-install-recommends && \
    rm -rf /var/lib/apt/lists/* && \
    sed -i 's/^# *\(de_DE.UTF-8\)/\1/' /etc/locale.gen && locale-gen && \
    ln -fs /usr/share/zoneinfo/Europe/Berlin /etc/localtime && \
    mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix && \
    useradd -m -d /home/mediaforge mediaforge && \
    mkdir -p /app/Downloads /home/mediaforge/.mediaforge /home/mediaforge/.aniworld && \
    chown -R mediaforge:mediaforge /app /home/mediaforge && \
    ln -s /tmp/.pki /home/mediaforge/.pki && \
    chown -h mediaforge:mediaforge /home/mediaforge/.pki

# .aniworld is only the mount point for legacy "AniWorld Downloader" volumes.
# Pre-creating it with the right ownership means users migrating from the old
# image can just add "- aniworld-data:/home/mediaforge/.aniworld:ro" to their
# compose file — no manual chown needed. legacy_import.py picks it up and
# copies the data on first boot; the folder stays empty and harmless otherwise.

# Container-friendly Python & UV defaults
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Default download directory
ENV MEDIAFORGE_DOWNLOAD_PATH=/app/Downloads \
    MEDIAFORGE_DOCKER=1

# Force Mesa software rendering (llvmpipe) for the captcha browser's WebGL.
# There is no GPU in a NAS/container, so Chromium would otherwise use its
# bundled SwiftShader -- a strong Turnstile bot signal. llvmpipe is a common,
# internally-consistent software renderer that looks far less automated.
# The matching Chromium flags live in playwright/captcha.py (_stealth_launch_args);
# disable the whole scheme with MEDIAFORGE_NO_LLVMPIPE=1 if it misbehaves.
ENV LIBGL_ALWAYS_SOFTWARE=1 \
    GALLIUM_DRIVER=llvmpipe

# Crashpad needs a writable database dir. Under a read-only container rootfs
# (docker-compose read_only: true) Chromium cannot create ~/.config/.../Crashpad,
# so it spawns chrome_crashpad_handler without --database and dies with SIGTRAP
# on startup. Point XDG config/cache at the writable /tmp tmpfs to fix it.
ENV XDG_CONFIG_HOME=/tmp/.config \
    XDG_CACHE_HOME=/tmp/.cache \
    XDG_DATA_HOME=/tmp/.local/share

# Realistic locale / timezone so the captcha browser doesn't look like a bare
# UTC server (Turnstile evaluates these signals).
ENV TZ=Europe/Berlin \
    LANG=de_DE.UTF-8 \
    LANGUAGE=de_DE:de \
    LC_ALL=de_DE.UTF-8

# Install dependencies & patchright browsers to a global path accessible by the unprivileged runtime user.
# This step is intentionally placed BEFORE copying source code so that the heavy
# dependency resolution and Chromium download are cached independently and only re-run when pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock README.md LICENSE MANIFEST.in /app/
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN uv sync --frozen --no-dev --no-install-project && \
    patchright install chromium && \
    chmod -R 755 /opt/ms-playwright /app/.venv

# Copy source and install the full project
COPY --chown=mediaforge:mediaforge src/ /app/src/
RUN uv sync --frozen --no-dev && \
    chown -R mediaforge:mediaforge /app/.venv /app/src

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
