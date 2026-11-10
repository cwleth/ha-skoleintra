FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 TZ=Europe/Copenhagen
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/skoleintra
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

CMD ["python", "-m", "app.main"]
