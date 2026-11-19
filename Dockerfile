# 默认经 DaoCloud 代理拉取 Docker Hub 官方 python 镜像（国内网络常用）；直连 Hub 可改回官方。
# 集成方若能访问 Docker Hub：docker build --build-arg PYTHON_BASE=python:3.11-slim ...
ARG PYTHON_BASE=docker.m.daocloud.io/library/python:3.11-slim
FROM ${PYTHON_BASE}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Prevent Python from writing .pyc files, buffer stdout/stderr, and pin common tooling paths
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/root/.local/bin:${PATH}" \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DM_HOME=/opt/dmdbms \
    LD_LIBRARY_PATH=/opt/dmdbms/bin

# Install system dependencies required by scientific Python stack, Playwright, Streamlit, and WeasyPrint PDF
# Prefer China Debian mirrors (deb.debian.org is often flaky from domestic networks).
RUN set -euo pipefail; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i 's|https\?://deb.debian.org|http://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
      sed -i 's|https\?://deb.debian.org|http://mirrors.aliyun.com|g' /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    if apt-cache show libgdk-pixbuf-2.0-0 >/dev/null 2>&1; then \
        GDK_PIXBUF_PKG=libgdk-pixbuf-2.0-0; \
    else \
        GDK_PIXBUF_PKG=libgdk-pixbuf2.0-0; \
    fi; \
    apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        git \
        libaio1t64 \
        libaio-dev \
        libgl1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libpangoft2-1.0-0 \
        "${GDK_PIXBUF_PKG}" \
        libffi-dev \
        libcairo2 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxi6 \
        libxtst6 \
        libnss3 \
        libxrandr2 \
        libxkbcommon0 \
        libasound2 \
        libx11-xcb1 \
        libxshmfence1 \
        libgbm1 \
        ffmpeg; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# Install the latest uv release and expose it on PATH
RUN curl -LsSf --retry 3 --retry-delay 2 --proto '=https' --proto-redir '=https' --tlsv1.2 https://astral.sh/uv/install.sh | sh

# ============================================
# 切换到驱动目录
WORKDIR /opt/dmdbms/bin

# 将本地的 dm_drivers 文件夹复制进去 (请确保项目根目录下有此文件夹)
COPY dm_drivers/ .

# 赋予执行权限
RUN chmod 755 *.so* || true

WORKDIR /app

# Install Python dependencies first to leverage Docker layer caching
COPY requirements.txt ./
# 默认走清华 PyPI 镜像，避免 files.pythonhosted.org 在校园网等环境下超时。
# 集成方用官方源：docker build --build-arg UV_INDEX=https://pypi.org/simple ...
ARG UV_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ENV UV_INDEX_URL=${UV_INDEX} \
    UV_HTTP_RETRIES=15 \
    UV_HTTP_TIMEOUT=300
# 不使用 cache mount：在部分 Docker Desktop/WSL 磁盘上，大 wheel 解压到 cache 卷易触发 I/O error。
# Linux 上默认 PyPI 会解析 CUDA 版 torch（数 GB 级 nvidia-* 依赖），构建极慢且无 GPU 可用。
# 策略：torch 用 pip 只走 CPU 源；其余依赖用 uv + constraint 锁定；最后再强制钉回 CPU torch。
# amd64 CPU 源版本名是 2.4.1+cpu；arm64 同索引没有该本地版，只有 2.4.1（见报错 versions 列表）。
ARG TARGETARCH
ARG TORCH_CPU_INDEX=https://download.pytorch.org/whl/cpu
ARG TORCH_CPU_VERSION=
RUN set -euo pipefail; \
    ARCH="${TARGETARCH:-}"; \
    if [ -z "${ARCH}" ]; then \
      case "$(uname -m)" in \
        aarch64|arm64) ARCH=arm64 ;; \
        *) ARCH=amd64 ;; \
      esac; \
    fi; \
    if [ -n "${TORCH_CPU_VERSION:-}" ]; then \
      TORCH_VER="${TORCH_CPU_VERSION}"; \
    elif [ "${ARCH}" = "arm64" ]; then \
      TORCH_VER="2.4.1"; \
    else \
      TORCH_VER="2.4.1+cpu"; \
    fi; \
    echo "${TORCH_VER}" > /tmp/torch-version.txt; \
    echo "Using torch==${TORCH_VER} (ARCH=${ARCH})"; \
    grep -v '^torch' requirements.txt > /tmp/requirements-no-torch.txt; \
    echo "torch==${TORCH_VER}" > /tmp/torch-constraint.txt

# pip 只连 PyTorch CPU 源（避免 uv 先命中清华 PyPI 上的 CUDA 版 torch）
# 大 wheel 在外场易超时：优先装本地 wheels/ 下的 torch*.whl；否则拉长超时多重点试。
# 预下载说明见 wheels/README.md
COPY wheels/ /tmp/torch-wheels/
RUN set -euo pipefail; \
    TORCH_VER="$(cat /tmp/torch-version.txt)"; \
    shopt -s nullglob; \
    LOCAL_WHLS=(/tmp/torch-wheels/torch*.whl); \
    if [ "${#LOCAL_WHLS[@]}" -gt 0 ]; then \
      echo "Installing torch from local wheel: ${LOCAL_WHLS[*]}"; \
      pip install --no-cache-dir "${LOCAL_WHLS[@]}"; \
    else \
      echo "No local torch wheel; downloading torch==${TORCH_VER} from ${TORCH_CPU_INDEX}"; \
      pip install --no-cache-dir \
        --default-timeout=3600 \
        --retries 15 \
        --index-url "${TORCH_CPU_INDEX}" \
        "torch==${TORCH_VER}"; \
    fi

# 其余依赖：constraint 锁定 torch 版本；uv 允许跨索引匹配 CPU 版 torch
RUN set -euo pipefail; \
    TORCH_VER="$(cat /tmp/torch-version.txt)"; \
    uv pip install --system \
    --index-strategy unsafe-best-match \
    --index-url "${UV_INDEX}" \
    --extra-index-url "${TORCH_CPU_INDEX}" \
    -r /tmp/requirements-no-torch.txt \
    -c /tmp/torch-constraint.txt; \
    shopt -s nullglob; \
    LOCAL_WHLS=(/tmp/torch-wheels/torch*.whl); \
    if [ "${#LOCAL_WHLS[@]}" -gt 0 ]; then \
      pip install --no-cache-dir --force-reinstall --no-deps "${LOCAL_WHLS[@]}"; \
    else \
      pip install --no-cache-dir --force-reinstall --no-deps \
        --default-timeout=3600 \
        --retries 15 \
        --index-url "${TORCH_CPU_INDEX}" \
        "torch==${TORCH_VER}"; \
    fi

# Install Playwright browser binaries (system deps already handled above)
RUN python -m playwright install chromium

# Copy .env
COPY .env.example .env

# Copy application source
COPY . .

# Ensure runtime directories exist even if ignored in build context
RUN mkdir -p /ms-playwright logs final_reports insight_engine_streamlit_reports media_engine_streamlit_reports query_engine_streamlit_reports

EXPOSE 5000 8501 8502 8503

# Default command launches the Flask orchestrator which starts Streamlit agents
CMD ["python", "app.py"]
