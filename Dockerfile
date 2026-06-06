# ─── Multi-Stage/Slim Production Dockerfile ───
FROM python:3.10-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=5000

# Set working directory
WORKDIR /app

# Install minimal compiler dependencies (needed for certain binary extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency requirements first to leverage Docker layer caching
COPY requirements.txt /app/

# Install dependencies and clean pip cache to minimize image size
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy the application source code into the container
COPY . /app/

# Expose target port
EXPOSE 5000

# Run with Gunicorn production server binding to dynamic Render PORT
CMD gunicorn --bind 0.0.0.0:$PORT --timeout 300 server:app
