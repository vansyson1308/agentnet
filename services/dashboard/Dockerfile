FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV REGISTRY_URL=http://registry:8000
ENV PAYMENT_URL=http://payment:8001
ENV DASHBOARD_PORT=8080

EXPOSE 8080

CMD ["python", "-m", "app.main"]
