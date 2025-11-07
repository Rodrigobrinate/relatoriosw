# 1. Imagem Base
FROM python:3.10-slim

# 2. Instalar o CRON
RUN apt-get update && apt-get install -y cron \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 3. Criar o diretório da aplicação
WORKDIR /app

# 4. Instalar dependências Python
COPY requeriments.txt .
RUN pip install -r requeriments.txt

# 5. Copiar SEUS SCRIPTS e TUDO MAIS
COPY . .

# *** NOVA LINHA ***
# Cria um diretório para os logs separados
RUN mkdir /app/logs

# 6. Configurar o CRON
COPY meus-jobs-cron /etc/cron.d/meus-jobs-cron
RUN chmod 0644 /etc/cron.d/meus-jobs-cron
RUN crontab /etc/cron.d/meus-jobs-cron
RUN  /usr/local/bin/python -m prisma db push
RUN  /usr/local/bin/python -m prisma generate

# 7. Comando para Iniciar
CMD ["cron", "-f"]