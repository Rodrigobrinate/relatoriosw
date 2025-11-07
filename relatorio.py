#!/usr/bin/env python3
import requests
import paramiko
import time
import socket
import re
import pprint
import asyncio  # <-- Importante para o Prisma
from prisma import Prisma # <-- O novo cliente de banco de dados
from prisma.models import Device, NetworkInterface

# --- 1. CONFIGURAÇÕES ---
# (As configs de banco de dados agora estão no arquivo .env)

# Configs do LibreNMS
LIBRENMS_URL = "http://45.165.244.43"
LIBRENMS_API_TOKEN = "801bea998c08323b26b6e6b61de7f9cc"


# Credenciais SSH
SSH_USERNAME = "zabbix.view"
SSH_PASSWORD = "view@123"

# Comando a ser executado
SSH_COMMAND = "display interface description"

# --- 2. LÓGICA DO LIBRENMS ---
# (Esta função permanece IDÊNTICA)
def get_huawei_devices():
    """Busca dispositivos no LibreNMS e filtra por 'icon' em Python."""
    headers = {'X-Auth-Token': LIBRENMS_API_TOKEN}
    
    # 1. Mudamos o endpoint. Em vez de tentar filtrar pela API,
    #    buscamos todos os dispositivos "ativos".
    #    Se você tiver muitos dispositivos, pode remover o "?type=active"
    #    para buscar TODOS (incluindo os desativados).
    endpoint = f"{LIBRENMS_URL}/api/v0/devices?type=up&limit=10"
    
    print(f"Buscando TODOS os dispositivos ativos em {endpoint}...")
    try:
        response = requests.get(endpoint, headers=headers, timeout=30) # Aumentei o timeout
        response.raise_for_status() 
        
        data = response.json()
        if data['status'] != 'ok' or not data['devices']:
            print("Nenhum dispositivo encontrado ou erro na API.")
            return []

        all_devices = data['devices']
        #all_devices = all_devices[0:5]
        print(f"Recebidos {len(all_devices)} dispositivos. Filtrando por icon='huawei.svg'...")
        
        # 2. A MÁGICA ESTÁ AQUI: Filtramos a lista no Python
        filtered_devices = []
        for dev in all_devices:
            # Verificamos se a chave 'icon' existe e se o valor é o esperado
            if dev.get('icon') == 'huawei.svg':
                filtered_devices.append(dev)
        
        if not filtered_devices:
            print("Nenhum dispositivo com o ícone 'huawei.svg' foi encontrado.")
        else:
            print(f"Filtrados! Encontrados {len(filtered_devices)} dispositivos com o ícone 'huawei.svg'.")
            
        return filtered_devices
            
    except requests.exceptions.RequestException as e:
        print(f"Erro ao conectar no LibreNMS API: {e}")
        return []

# --- 3. LÓGICA DO SSH ---
# (Esta função permanece IDÊNTICA)
def get_ssh_output(host, username, password, command):
    """
    Conecta via SSH, executa um comando lendo até o prompt,
    e só então envia 'quit'. Isso evita misturar saídas.
    """
    print(f"Executando SSH em {host} para o comando: '{command[:30]}...'")
    output = ""
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host, port=22, username=username, password=password,
            look_for_keys=False, allow_agent=False, banner_timeout=200, timeout=10
        )
    except Exception as e:
        print(f"Falha ao conectar SSH em {host}: {e}")
        return None
    
    try:
        channel = client.invoke_shell()
        # Aumentamos o timeout de leitura, pois esperamos o prompt
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
        
        # Damos um tempo para o comando executar e começar a cuspir dados
        time.sleep(1.0) 
        
        # 4. Loop de leitura ATÉ VER O PROMPT
        while True:
            try:
                if channel.recv_ready():
                    chunk = channel.recv(65535)
                    if not chunk:
                        break # Canal fechou
                    output += chunk.decode('latin-1')
                    
                    # VERIFICAÇÃO DO PROMPT:
                    # Se os últimos caracteres (sem espaços) forem '>' (Huawei) ou ']' (Huawei VRP)
                    # assumimos que o comando terminou.
                    stripped_output = output.strip()
                    if stripped_output.endswith('>') or stripped_output.endswith(']'):
                        # print("  [d] Prompt detectado. Parando leitura.")
                        break
                
                if channel.exit_status_ready():
                    break
                    
                time.sleep(0.2) # Não sobrecarregar a CPU
                
            except socket.timeout:
                # Deu timeout. Assumimos que o comando terminou.
                # print("  [d] Socket timeout. Assumindo fim de comando.")
                break
        
        # 5. Agora que lemos tudo, enviamos o QUIT
        channel.send('quit\n')
        time.sleep(0.5)
        client.close()
        
        # 6. Limpeza da Saída
        # O 'output' contém o eco do comando (1ª linha) e o prompt final (última linha).
        lines = output.splitlines()
        
        if len(lines) <= 2:
            return "" # Não recebemos dados

        # Filtra a primeira linha (eco do comando) e a última linha (prompt)
        clean_lines = []
        for line in lines[1:-1]: # Pula a primeira e a última
            if line.strip(): # Adiciona só se não for linha em branco
                clean_lines.append(line)

        return "\n".join(clean_lines)

    except Exception as e:
        print(f"Erro durante a execução do comando SSH em {host}: {e}")
        if 'client' in locals() and client:
            client.close()
        return None
# --- 4. LÓGICA DE PARSING ---
# (Esta função permanece IDÊNTICA)
def parse_output(output_text: str) -> list:
    """
    Analisa a saída de 'display interface description'.
    Esta versão usa um regex simplificado que captura corretamente
    linhas com e sem descrição.
    """
    
    # --- REGEX CORRIGIDO ---
    #
    # MUDANÇA:
    # O Grupo 4 (Descrição) agora é simplesmente (.*)$
    # Isso significa "capture todo o resto da linha".
    #
    regex_pattern = re.compile(
        # Início da linha
        r"^"
        
        # Grupo 1: (Interface_Name)
        # SÓ captura portas físicas
        r"((?:XGED|XGE|GE|40GE|100GE|Meth)\d+/\d+/\d+)"
        
        # Separador
        r"\s+"
        
        # Grupo 2: (Physical_Status)
        r"(up|down|\*down)"
        
        # Separador
        r"\s+"
        
        # Grupo 3: (Protocol_Status)
        r"(up|down|\*down)"
        
        # Grupo 4: (Description)
        # Captura "o resto da linha".
        # Se houver descrição, captura '  LACP...'.
        # Se não houver, captura ' ' (string vazia).
        r"(.*)$"
        ,
        re.MULTILINE
    )
    # --- FIM DO REGEX ---
    
    dados_finais = []
    
    matches = regex_pattern.finditer(output_text)

    for match in matches:
        interface = match.group(1)
        status_fisico = match.group(2)
        status_admin = match.group(3)
        
        # O group(4) vai pegar '  LACP...' ou ''.
        # O .strip() limpa ambos os casos perfeitamente.
        descricao = match.group(4).strip()
        
        dados_finais.append({
            "interface_name": interface,
            "physical_status": status_fisico,
            "protocol_status": status_admin,
            "description": descricao
        })

    return dados_finais

# --- 5. LÓGICA DO BANCO DE DADOS (NOVA: com Prisma) ---

async def sync_database(devices_from_api, interfaces_data_map):
    """
    Sincroniza os dados do LibreNMS e das interfaces com o banco de dados
    usando o Prisma Client.
    """
    db = Prisma()
    
    print("\nIniciando sincronização com o banco de dados (Prisma)...")
    #devices_from_api = devices_from_api[0:5]
    
    try:
        await db.connect()
        
        # --- PASSO A: Sincronizar Dispositivos ---
        print("Sincronizando dispositivos...")
        for dev in devices_from_api:
            # Lógica UPSERT do Prisma para dispositivos
            device = await db.device.upsert(
                where={'hostname': dev['hostname']},
                data={
                    'create': {
                        'librenms_device_id': dev['device_id'],
                        'hostname': dev['sysName'],
                        'ip_address': dev['ip'],
                        'os': dev.get('os'),
                        'vendor': dev.get('vendor')
                    },
                    'update': {
                        'hostname': dev['sysName'],
                        'librenms_device_id': dev['device_id'],
                        'ip_address': dev['ip'],
                        'os': dev.get('os'),
                        'vendor': dev.get('vendor')
                        # 'last_polled' é atualizado automaticamente via @updatedAt
                    }
                }
            )
            
            # --- PASSO B: Sincronizar Interfaces ---
            if dev['hostname'] not in interfaces_data_map:
                continue
            
            print(f"Sincronizando interfaces para {dev['hostname']} (DB ID: {device.id})...")
            
            interfaces = interfaces_data_map[dev['hostname']]
            if not interfaces:
                continue
                
            for iface in interfaces:
                # Lógica UPSERT do Prisma para interfaces
                # Nota: a chave única é 'device_id_interface_name'
                await db.networkinterface.upsert(
                    where={
                        'device_id_interface_name': {
                            'device_id': device.id,
                            'interface_name': iface['interface_name']
                        }
                    },
                    data={
                        'create': {
                            'device_id': device.id, # Link para o dispositivo
                            'interface_name': iface['interface_name'],
                            'physical_status': iface['physical_status'],
                            'protocol_status': iface['protocol_status'],
                            'description': iface['description']
                        },
                        'update': {
                            'physical_status': iface['physical_status'],
                            'protocol_status': iface['protocol_status'],
                            'description': iface['description']
                            # 'last_updated' é atualizado automaticamente via @updatedAt
                        }
                    }
                )
        
        print("Sincronização com o banco de dados concluída com sucesso.")
        
    except Exception as e:
        print(f"Erro durante a transação com o banco de dados: {e}")
    finally:
        if db.is_connected():
            await db.disconnect()

# --- 6. ORQUESTRAÇÃO (MAIN) ---
# (Agora é uma função 'async' para usar o Prisma)

async def main():
    # 1. Buscar dispositivos no LibreNMS (Síncrono)
    devices = get_huawei_devices()
    if not devices:
        print("Nenhum dispositivo encontrado. Encerrando.")
        return

    interfaces_data_map = {}

    # 2. Coletar dados de cada dispositivo (Síncrono)
    for dev in devices:
        host = dev['ip'] 
        hostname = dev['hostname']
        
        raw_output = get_ssh_output(host, SSH_USERNAME, SSH_PASSWORD, SSH_COMMAND)
        
        if raw_output:
            parsed_data = parse_output(raw_output)
            if parsed_data:
                print(f"Sucesso! Parseadas {len(parsed_data)} interfaces de {hostname}.")
                interfaces_data_map[hostname] = parsed_data
            else:
                print(f"Comando executado em {hostname}, mas nenhum dado de interface foi parseado.")
        else:
            print(f"Não foi possível coletar dados de {hostname}.")

    # 3. Salvar tudo no banco de dados (Assíncrono)
    if interfaces_data_map:
        await sync_database(devices, interfaces_data_map)
    else:
        print("Nenhum dado de interface foi coletado. Nada para salvar no banco.")

if __name__ == "__main__":
    # Inicia o loop de eventos asyncio para rodar a função main
    asyncio.run(main())