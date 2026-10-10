# Образ бота «Настройка Cardinal с ассистентом».
# Сборка (из корня проекта): docker compose -f deploy/docker-compose.yml up -d --build
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py steps.py ./

# Токен и настройки передаются через переменные окружения (env_file в compose).
CMD ["python", "bot.py"]
