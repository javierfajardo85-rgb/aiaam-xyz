FROM python:3.11-slim

WORKDIR /app

# Install deps first — better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

EXPOSE 8080

# Use shell form so ${PORT:-8080} expands at runtime (Railway injects $PORT)
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
