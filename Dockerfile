FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Fewer malloc arenas: the thread pool otherwise keeps freed memory per thread.
ENV ZIM_DIR=/data/zim MALLOC_ARENA_MAX=2 PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
