FROM python:3.11-slim

WORKDIR /app

# Instalar dependencias de Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar Playwright y navegador Chromium
RUN playwright install --with-deps chromium

# Copiar los archivos del proyecto
COPY . .

# Comando para iniciar checker2.py
CMD ["python", "checker2.py"]