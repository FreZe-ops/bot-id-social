FROM python:3.12-slim

WORKDIR /app

ENV TZ=Asia/Ho_Chi_Minh \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata libatomic1 \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .

CMD ["python", "main.py"]
