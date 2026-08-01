FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# 先裝相依（善用 layer 快取）
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt

# 複製專案
COPY . .

EXPOSE 8000
# 首次啟動自動建表＋灌題庫，再啟動 API
CMD ["python", "deploy/start.py"]
