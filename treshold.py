#!/usr/bin/env python3
import paramiko
import time
import socket
import re
import asyncio
from prisma import Prisma
from prisma.models import Device, NetworkInterface

# --- 1. CONFIGURAÇÕES ---
# (As configs de banco de dados vêm do .env)

# Credenciais SSH (Usadas para TODOS os dispositivos)
SSH_USERNAME = "zabbix.view"
SSH_PASSWORD = "view@123"

# --- 2. LÓGICA DO SSH (ROBUSTA) ---
# Esta é a versão final e corrigida da função SSH,
# que detecta o prompt para evitar misturar saídas.
def get_ssh_output(host, username, password, command):
    """
    Conecta via SSH, executa um comando lendo até o prompt,
    e só então envia 'quit'. Isso evita misturar saídas.
    """
    print(f"  -> SSH: Executando em {host}: '{command[:35]}...'")
    output = ""
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host, port=22, username=username, password=password,
            look_for_keys=False, allow_agent=False, banner_timeout=200, timeout=10
        )
    except Exception as e:
        print(f"    [!] Falha ao conectar SSH em {host}: {e}")
        return None
    
    try:
        channel = client.invoke_shell()
        channel.settimeout(10.0) 

        # 1. Limpa o banner inicial
        time.sleep(1)
        try: channel.recv(65535)
        except socket.timeout: pass
        
        # 2. Desliga paginação
        channel.send('screen-length 0 temporary\n')
        time.sleep(0.5)
        try: channel.recv(65535)
        except socket.timeout: pass
        
        # 3. Envia o comando principal
        channel.send(command + '\n')
        time.sleep(1.0) 
        
        # 4. Loop de leitura ATÉ VER O PROMPT
        while True:
            try:
                if channel.recv_ready():
                    chunk = channel.recv(65535)
                    if not chunk: break 
                    output += chunk.decode('latin-1')
                    
                    # Detecção de prompt
                    stripped_output = output.strip()
                    if stripped_output.endswith('>') or stripped_output.endswith(']'):
                        break
                
                if channel.exit_status_ready(): break
                time.sleep(0.2)
                
            except socket.timeout:
                break
        
        # 5. Agora que lemos tudo, enviamos o QUIT
        channel.send('quit\n')
        time.sleep(0.5)
        client.close()
        
        # 6. Limpeza da Saída
        # Filtra a primeira linha (eco do comando) e a última linha (prompt)
        lines = output.splitlines()
        if len(lines) <= 2:
            return "" 

        clean_lines = []
        for line in lines[1:-1]:
            if line.strip():
                clean_lines.append(line)

        return "\n".join(clean_lines)

    except Exception as e:
        print(f"    [!] Erro durante a execução do comando SSH em {host}: {e}")
        if 'client' in locals() and client:
            client.close()
        return None

# --- 3. LÓGICA DE PARSING (THRESHOLD) ---

def build_transceiver_command(interface_name: str) -> str | None:
    """
    Cria o comando dinâmico para buscar dados do transceiver.
    Filtra interfaces lógicas (Vlanif, Eth-Trunk, etc.)
    """
    # Regex para separar o tipo (letras) do nome (números/barras)
    match = re.match(r"^([A-Za-z-]+)(\d+.*)$", interface_name)
    
    if match:
        if_type = match.group(1)
        if_name = match.group(2)
        
        # FILTRO: Ignora interfaces que não são portas físicas
        # (Esta é a mesma regra de filtro do relatorio.py)
        if if_type.lower() in ["loopback", "vlanif", "eth-trunk", "null", "vlan-interface"]:
            return None
            
        # Retorna o comando completo
        return f"dis transceiver interface {if_type} {if_name} verbose | include Threshold"
    
    # Se não deu match (ex: "Vlanif100"), não é uma porta física
    return None

def parse_thresholds(output_text: str) -> dict:
    """Analisa a saída do comando de transceiver e extrai os 10 valores."""
    
    # Mapa de chaves: O texto da Huawei -> O nome do campo no nosso DB
    key_map = {
        'Temp High Threshold': 'temp_high',
        'Temp Low Threshold': 'temp_low',
        'Volt High Threshold': 'volt_high',
        'Volt Low Threshold': 'volt_low',
        'Bias High Threshold': 'bias_high',
        'Bias Low Threshold': 'bias_low',
        'RX Power High Threshold': 'rx_power_high',
        'RX Power Low Threshold': 'rx_power_low',
        'TX Power High Threshold': 'tx_power_high',
        'TX Power Low Threshold': 'tx_power_low'
    }
    
    # Regex: Captura a (Chave) e o (Valor numérico, possivelmente negativo)
    regex = re.compile(r"^\s+([\w\s]+?)[\s(]+.*:\s*(-?[\d.]+)\s*$", re.MULTILINE)
    
    thresholds = {}
    
    for match in regex.finditer(output_text):
        key_text = ' '.join(match.group(1).split())
        value_text = match.group(2)
        
        if key_text in key_map:
            db_field = key_map[key_text]
            try:
                thresholds[db_field] = float(value_text)
            except ValueError:
                print(f"    [!] Aviso: Valor não numérico para '{key_text}': {value_text}")

    return thresholds

# --- 4. ORQUESTRAÇÃO (MAIN) ---

async def main():
    db = Prisma()
    
    print("Iniciando script de coleta de thresholds...")
    print("Lendo dispositivos e interfaces do banco de dados...")
    
    try:
        await db.connect()
        
        # 1. Busca no DB todos os dispositivos
        devices = await db.device.find_many()
        
        if not devices:
            print("[ERRO] Nenhum dispositivo encontrado no banco de dados.")
            print("Execute o script 'relatorio.py' primeiro.")
            return

        print(f"Encontrados {len(devices)} dispositivos no banco de dados para verificar.")
        
        # 2. Itera sobre cada dispositivo
        for dev in devices:
            print(f"\n--- Processando Dispositivo: {dev.hostname} (IP: {dev.ip_address}) ---")
            
            # 3. Busca todas as interfaces DESTE dispositivo no DB
            interfaces = await db.networkinterface.find_many(
                where={'device_id': dev.id}
            )
            
            if not interfaces:
                print(f"  Nenhuma interface encontrada para {dev.hostname} no DB.")
                continue
                
            print(f"  Encontradas {len(interfaces)} interfaces. Verificando transceivers...")
            
            interfaces_processed = 0
            
            # 4. Itera sobre cada interface e coleta os dados
            for iface in interfaces:
                # 4.1. Constrói o comando
                command = build_transceiver_command(iface.interface_name)
                
                # Se 'None', é uma porta virtual (Vlanif, etc), pulamos
                if command is None:
                    continue
                
                interfaces_processed += 1
                
                # 4.2. Executa o SSH (uma conexão por interface)
                raw_output = get_ssh_output(dev.ip_address, SSH_USERNAME, SSH_PASSWORD, command)
                
                if not raw_output:
                    continue
                    
                # 4.3. Analisa (parse) os dados
                threshold_data = parse_thresholds(raw_output)
                
                if threshold_data:
                    # 4.4. Salva os dados NO BANCO (faz o UPDATE)
                    print(f"    [OK] Coletado threshold para {iface.interface_name}. Salvando...")
                    await db.networkinterface.update(
                        where={'id': iface.id},
                        data=threshold_data
                    )
            
            if interfaces_processed == 0:
                print("  Nenhuma interface física (com transceiver) encontrada para este dispositivo.")
        
        print("\n==============================================")
        print("Coleta de thresholds concluída para todos os dispositivos.")
        
    except Exception as e:
        print(f"\n[ERRO FATAL] Ocorreu um erro: {e}")
    finally:
        if db.is_connected():
            await db.disconnect()
            print("Desconectado do banco de dados.")

if __name__ == "__main__":
    asyncio.run(main())