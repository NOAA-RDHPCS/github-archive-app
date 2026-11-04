FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Create non-root user
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home appuser


ENV DEBIAN_FRONTEND=noninteractive

RUN set -eux; \
    # 1. Swap http for https in the new DEB822-format configuration file
    sed -i 's|http://|https://|g' /etc/apt/sources.list.d/debian.sources; \
    \
    # 2. Update the cache using the new HTTPS URIs and immediately install ca-certificates
    #    (Note: Debian base images usually bundle ca-certificates, but installing explicitly guarantees verification)
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates; \
    \
    # 3. Proceed with your standard python package/system dependencies
    apt-get install -y --no-install-recommends \
        curl \
        build-essential; \
    \
    # 4. Clean up cache layers to keep the slim image optimized
    rm -rf /var/lib/apt/lists/*


# Install system dependencies
#RUN apt-get update && apt-get install -y --no-install-recommends \
#    curl \
#    && rm -rf /var/lib/apt/lists/*


# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create archive directory
RUN mkdir -p /var/lib/github-archive/json && \
    chown -R appuser:appuser /var/lib/github-archive

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
