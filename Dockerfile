FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py telegram_user.py .
RUN mkdir -p /app/data

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:api --host 0.0.0.0 --port ${PORT:-8000}"]
