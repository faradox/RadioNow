FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
ARG USER_ID=1000
ARG GROUP_ID=1000
RUN groupadd -g ${GROUP_ID} radionow && \
    useradd -u ${USER_ID} -g radionow -m radionow

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/

# Create directories for mounted volumes
RUN mkdir -p /app/voices /app/audio && \
    chown -R radionow:radionow /app

USER radionow

EXPOSE 8090

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
