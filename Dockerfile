FROM python:3.11-slim

WORKDIR /app

# Set Python to run in unbuffered mode for immediate log output
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application files
COPY . .

# Run the ingestion script (default, can be overridden in docker-compose.yml)
CMD ["python", "main.py"]

