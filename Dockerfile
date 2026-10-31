FROM python:3.15-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

ENV FLASK_APP=app.py

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--worker-class", "gthread", "--threads", "8", "--timeout", "600", "app:app"]
