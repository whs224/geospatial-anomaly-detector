FROM python:3.11-slim-bookworm

WORKDIR /app

# Unbuffered output so container logs stream immediately
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --shell /usr/sbin/nologin app
USER app

# Ingestion is the default; docker-compose.yml overrides per service
CMD ["python", "main.py"]
