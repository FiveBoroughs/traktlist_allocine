FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY allocine.py trakt_client.py publishers.py resolver.py main.py scheduler.py sync.sh ./
RUN chmod +x sync.sh

CMD ["python", "scheduler.py"]
