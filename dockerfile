# 1. Imagem Base
FROM python:3.10-slim

# 2. Instalar o CRON
RUN apt-get update && apt-get install -y cron \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 3. Criar o diretório da aplicação
WORKDIR /app

# 4. Instalar dependências Python
COPY requirements.txt .
RUN pip install -r requirements.txt 


# 5. Copiar SEUS SCRIPTS e TUDO MAIS
# O comando "COPY . ." já copia TODOS os arquivos (relatorio.py, treshold.py, etc.)
COPY . .

# 6. Configurar o CRON (AQUI É A CORREÇÃO)
# Copie o SEU ARQUIVO de agendamento (que vamos criar abaixo)
# para o diretório de configuração do cron.
COPY meus-jobs-cron.txt /etc/cron.d/meus-jobs-cron.txt

# Dê a permissão correta para o arquivo de agendamento
RUN chmod 0644 /etc/cron.d/meus-jobs-cron

# Crie um "crontab" a partir do arquivo (passo de segurança)
RUN crontab /etc/cron.d/meus-jobs-cron

# 7. Comando para Iniciar
# Inicia o daemon do cron em modo "foreground"
CMD ["cron", "-f"]