#!/usr/bin/env python3
import paramiko
import sys
import argparse
import time
import socket

def main():
    parser = argparse.ArgumentParser(
        description='Executa um comando em um switch Huawei via SSH usando um shell interativo'
    )
    parser.add_argument('--host',     required=True, help='IP ou hostname do switch')
    parser.add_argument('--port',     type=int, default=22, help='Porta SSH (padrão: 22)')
    parser.add_argument('--username', required=True, help='Usuário SSH')
    parser.add_argument('--password', required=True, help='Senha SSH')
    parser.add_argument('--command',  required=True, help='Comando a executar')
    args = parser.parse_args()

    # Cria o cliente SSH e aceita hosts desconhecidos
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=args.host,
        port=args.port,
        username=args.username,
        password=args.password,
        look_for_keys=False,
        allow_agent=False,
        banner_timeout=200,
        timeout=10
    )

    # Inicia um shell interativo
    channel = client.invoke_shell()
    channel.settimeout(2.0)

    # Pequena pausa para o banner inicial
    time.sleep(1)
    try:
        channel.recv(65535)
    except socket.timeout:
        pass

    # 1) Desliga a paginação
    channel.send('screen-length 0 temporary\n')
    time.sleep(0.5)
    try:
        channel.recv(65535)
    except socket.timeout:
        pass

    # 2) Envia o comando principal
    channel.send(args.command + '\n')
    time.sleep(1)

    # 3) Sai do shell
    channel.send('quit\n')
    # nota: em alguns HUAWEI pode ser 'exit' ou 'quit'

    # Lê tudo até o canal fechar ou timeout
    output = ''
    while True:
        try:
            if channel.recv_ready():
                chunk = channel.recv(65535)
                if not chunk:
                    break
                output += chunk.decode('utf-8', errors='ignore')
            else:
                # se saiu do shell, devemos ver exit_status
                if channel.exit_status_ready():
                    break
                time.sleep(0.1)
        except socket.timeout:
            # sem dados novos por timeout, encerramos
            break

    client.close()

    # Imprime saída para o Node.js capturar
    sys.stdout.write(output)

if __name__ == '__main__':
    main()

    