FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# System dependencies + Node.js
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        xvfb \
        libpq5 \
        libpq5
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt ./

RUN pip install --no-cache-dir -r requirements.txt

# Node dependencies
COPY package.json package-lock.json ./

RUN npm ci

# Install Playwright Chromium + required system dependencies
RUN python -m playwright install --with-deps chromium

# Application source
COPY . .

# Build Tailwind CSS
RUN npm run build:css

EXPOSE 8000

CMD ["sh", "-c", "python manage.py migrate && python manage.py collectstatic --noinput && python manage.py provision_admin && exec xvfb-run --auto-servernum --server-args='-screen 0 1440x900x24' gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000}"]