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

# !!! NOVA FLAG DE CONTROLE !!!
# False: Imprime os dados coletados no console (em JSON/dict).
# True: Salva os dados no banco de dados.
SAVE_TO_DATABASE = True

# Configs do LibreNMS
LIBRENMS_URL = "http://45.165.244.43"
LIBRENMS_API_TOKEN = "801bea998c08323b26b6e6b61de7f9cc"
# Ícone usado para filtrar dispositivos Juniper (ajuste se necessário)
LIBRENMS_OS_ICON = "junos.png" 

# Credenciais SSH
SSH_USERNAME = "zabbix.view"
SSH_PASSWORD = "view@123"

# Comando a ser executado (Juniper)
SSH_COMMAND = "show interfaces descriptions"

# --- 2. LÓGICA DO LIBRENMS (Adaptada para Juniper) ---

def get_juniper_devices():
    """Busca dispositivos no LibreNMS e filtra pelo ícone do Juniper."""
    headers = {'X-Auth-Token': LIBRENMS_API_TOKEN}
    
    endpoint = f"{LIBRENMS_URL}/api/v0/devices?type=up&limit=10"
    
    print(f"Buscando dispositivos ativos em {endpoint}...")
    try:
        response = requests.get(endpoint, headers=headers, timeout=30)
        response.raise_for_status() 
        
        data = response.json()
        if data['status'] != 'ok' or not data['devices']:
            print("Nenhum dispositivo encontrado ou erro na API.")
            return []

        all_devices = data['devices']
        print(f"Recebidos {len(all_devices)} dispositivos. Filtrando por icon='{LIBRENMS_OS_ICON}'...")
        
        # Filtramos a lista no Python
        filtered_devices = []
        for dev in all_devices:
            if dev.get('icon') == LIBRENMS_OS_ICON:
                filtered_devices.append(dev)
        
        if not filtered_devices:
            print(f"Nenhum dispositivo com o ícone '{LIBRENMS_OS_ICON}' foi encontrado.")
        else:
            print(f"Filtrados! Encontrados {len(filtered_devices)} dispositivos Juniper.")
            
        return filtered_devices
            
    except requests.exceptions.RequestException as e:
        print(f"Erro ao conectar no LibreNMS API: {e}")
        return []

# --- 3. LÓGICA DO SSH (Adaptada para Juniper) ---

def get_ssh_output(host, username, password, command):
    """
    Conecta via SSH, executa um comando (Junos) e lê a saída.
    """
    print(f"   -> [SSH] Executando em {host}: '{command[:30]}...'")
    output = ""
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host, port=22, username=username, password=password,
            look_for_keys=False, allow_agent=False, banner_timeout=200, timeout=10
        )
    except Exception as e:
        print(f"     [ERRO-SSH] Falha ao conectar SSH em {host}: {e}")
        return None
    
    try:
        channel = client.invoke_shell()
        channel.settimeout(10.0) 

        # 1. Limpa o banner inicial
        time.sleep(1)
        try: channel.recv(65535)
        except socket.timeout: pass
        
        # 2. Desliga paginação (Comando Juniper)
        channel.send('set cli screen-length 0\n')
        time.sleep(0.5)
        try: channel.recv(65535)
        except socket.timeout: pass
        
        # 3. Envia o comando principal
        channel.send(command + '\n')
        
        time.sleep(1.0) 
        
        start_time = time.time() # Timeout para o loop de leitura
        
        # 4. Loop de leitura ATÉ VER O PROMPT ('>' ou ']')
        while True:
            try:
                if channel.recv_ready():
                    chunk = channel.recv(65535)
                    if not chunk:
                        break 
                    output += chunk.decode('latin-1')
                    
                    # VERIFICAÇÃO DO PROMPT:
                    stripped_output = output.strip()
                    if stripped_output.endswith('>') or stripped_output.endswith(']'):
                        break
                
                if channel.exit_status_ready():
                    break
                
                if time.time() - start_time > 20.0:
                    print(f"     [ERRO-SSH] Timeout de 20s atingido esperando o comando em {host}")
                    break
                    
                time.sleep(0.2)
                
            except socket.timeout:
                pass
        
        # 5. Envia o QUIT
        channel.send('quit\n')
        time.sleep(0.5)
        client.close()
        
        # 6. Limpeza da Saída
        lines = output.splitlines()
        
        if len(lines) <= 2:
            return "" 

        # Filtra a primeira linha (eco do comando) e a última linha (prompt)
        clean_lines = []
        for line in lines[1:-1]:
            if line.strip() and not line.strip().startswith('{master'): # Ignora lixo do Junos
                clean_lines.append(line)

        return "\n".join(clean_lines)

    except Exception as e:
        print(f"     [ERRO-SSH] Erro durante a execução do comando SSH em {host}: {e}")
        if 'client' in locals() and client:
            client.close()
        return None
        
# --- 4. LÓGICA DE PARSING (Adaptada para Juniper) ---

def parse_output(output_text: str) -> list:
    """
    Analisa a saída de 'show interfaces descriptions' do Juniper,
    juntando descrições que quebram de linha (wrapped lines).
    """
    
    dados_finais = []
    current_entry = None
    
    # Regex para a LINHA INICIAL de uma interface (começa sem espaço)
    # Ex: et-0/0/9   up    up   LACP: ...
    line_start_regex = re.compile(
        # Grupo 1: (Interface_Name)
        r"^(\S+)\s+"
        
        # Grupo 2: (Admin_Status)
        r"(up|down)\s+"
        
        # Grupo 3: (Link_Status)
        r"(up|down)\s*"
        
        # Grupo 4: (Description) - Opcional, captura o resto
        r"(.*)$"
    )
    
    # Regex para uma LINHA DE CONTINUAÇÃO (começa com espaço)
    # Ex:          [100Gbps]
    continuation_regex = re.compile(
        r"^\s+(.+)$"
    )

    lines = output_text.splitlines()

    for line in lines:
        # Ignora a linha de header
        if line.lower().strip().startswith("interface"):
            continue
            
        start_match = line_start_regex.match(line)
        continuation_match = continuation_regex.match(line)

        if start_match:
            # É uma nova interface. Salva a anterior (se existir).
            if current_entry:
                # Limpa espaços duplicados antes de salvar
                current_entry["description"] = re.sub(r'\s+', ' ', current_entry["description"]).strip()
                dados_finais.append(current_entry)
            
            # Inicia a nova entrada
            current_entry = {
                "interface_name": start_match.group(1),
                "protocol_status": start_match.group(2), # "Admin"
                "physical_status": start_match.group(3), # "Link"
                "description": start_match.group(4).strip() # Inicia a descrição
            }
        
        elif continuation_match and current_entry:
            # É uma continuação da descrição anterior.
            # Adiciona a linha, separada por um espaço.
            line_content = continuation_match.group(1).strip()
            if line_content:
                current_entry["description"] += " " + line_content
        
        else:
            # Linha em branco ou formato inesperado.
            # Salva a entrada atual (se houver) e reseta.
            if current_entry:
                # Limpa espaços duplicados antes de salvar
                current_entry["description"] = re.sub(r'\s+', ' ', current_entry["description"]).strip()
                dados_finais.append(current_entry)
            current_entry = None

    # Adiciona a última entrada que estava sendo processada
    if current_entry:
        current_entry["description"] = re.sub(r'\s+', ' ', current_entry["description"]).strip()
        dados_finais.append(current_entry)

    return dados_finais

# --- 5. LÓGICA DO BANCO DE DADOS (Idêntica, não modificada) ---

async def sync_database(devices_from_api, interfaces_data_map):
    """
    Sincroniza os dados do LibreNMS e das interfaces com o banco de dados
    usando o Prisma Client.
    """
    db = Prisma()
    
    print("\n[DB] Iniciando sincronização com o banco de dados (Prisma)...")
    
    try:
        await db.connect()
        
        # --- PASSO A: Sincronizar Dispositivos ---
        print("[DB] Sincronizando dispositivos...")
        for dev in devices_from_api:
            
            device = await db.device.upsert(
                where={'librenms_device_id': dev['device_id']}, 
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
                        'ip_address': dev['ip'],
                        'os': dev.get('os'),
                        'vendor': dev.get('vendor')
                    }
                }
            )
            
            # --- PASSO B: Sincronizar Interfaces ---
            api_hostname_key = dev['hostname'] 
            if api_hostname_key not in interfaces_data_map:
                continue
            
            print(f"   [DB] Sincronizando interfaces para {device.hostname} (DB ID: {device.id})...")
            
            interfaces = interfaces_data_map[api_hostname_key]
            if not interfaces:
                continue
                
            for iface in interfaces:
                await db.networkinterface.upsert(
                    where={
                        'device_id_interface_name': {
                            'device_id': device.id,
                            'interface_name': iface['interface_name']
                        }
                    },
                    data={
                        'create': {
                            'device_id': device.id, 
                            'interface_name': iface['interface_name'],
                            'physical_status': iface['physical_status'],
                            'protocol_status': iface['protocol_status'],
                            'description': iface['description']
                        },
                        'update': {
                            'physical_status': iface['physical_status'],
                            'protocol_status': iface['protocol_status'],
                            'description': iface['description']
                        }
                    }
                )
        
        print("[DB] Sincronização com o banco de dados concluída com sucesso.")
        
    except Exception as e:
        print(f"[ERRO FATAL-DB] Erro durante a transação com o banco de dados: {e}")
    finally:
        if db.is_connected():
            await db.disconnect()
            print("[DB] Desconectado do banco de dados.")

# --- 6. ORQUESTRAÇÃO (Com lógica de flag) ---

async def process_device_interfaces(dev: dict, semaphore: asyncio.Semaphore) -> tuple | None:
    """
    Worker para coletar e parsear interfaces de UM dispositivo.
    (Função idêntica à original, não precisou de mudanças)
    """
    
    async with semaphore:
        host = dev['ip'] 
        hostname = dev['hostname']
        print(f"\n--- [COLETA] Iniciando: {hostname} (IP: {host}) ---")
        
        raw_output = None
        try:
            raw_output = await asyncio.to_thread(
                get_ssh_output, 
                host, 
                SSH_USERNAME, 
                SSH_PASSWORD, 
                SSH_COMMAND
            )
        except Exception as e:
            print(f"     [ERRO-THREAD] Erro ao executar get_ssh_output na thread para {hostname}: {e}")
            return None 

        if raw_output:
            parsed_data = parse_output(raw_output)
            if parsed_data:
                print(f"   [COLETA] Sucesso! Parseadas {len(parsed_data)} interfaces de {hostname}.")
                return (hostname, parsed_data)
            else:
                print(f"   [COLETA] Comando executado em {hostname}, mas nenhum dado de interface foi parseado.")
                return (hostname, [])
        else:
            print(f"   [COLETA] Não foi possível coletar dados de {hostname}.")
            return None


async def main():
    MAX_CONCURRENT_TASKS = 30
    
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
    start_time = time.time()
    
    print(f"[INFO] Iniciando script de inventário Juniper (Paralelismo: {MAX_CONCURRENT_TASKS} devices)")
    print(f"[INFO] Modo de salvamento: {'BANCO DE DADOS' if SAVE_TO_DATABASE else 'APENAS CONSOLE (JSON)'}")

    # 1. Buscar dispositivos no LibreNMS
    print("[INFO] Buscando dispositivos do LibreNMS (em thread)...")
    try:
        devices = await asyncio.to_thread(get_juniper_devices) # <-- Chama a nova função
    except Exception as e:
        print(f"[ERRO FATAL] Falha ao buscar dispositivos do LibreNMS: {e}")
        return
        
    if not devices:
        print("[INFO] Nenhum dispositivo encontrado. Encerrando.")
        return

    print(f"[INFO] {len(devices)} dispositivos encontrados. Iniciando {len(devices)} tarefas de coleta SSH...")

    # 2. Criar lista de tarefas de coleta
    tasks = []
    for dev in devices:
        tasks.append(process_device_interfaces(dev, semaphore))

    # 3. Executar todas as tarefas de coleta simultaneamente
    results = await asyncio.gather(*tasks)

    # 4. Processar os resultados da coleta
    interfaces_data_map = {}
    devices_com_sucesso = []
    
    device_map = {dev['hostname']: dev for dev in devices}

    for result in results:
        if result:
            hostname, parsed_data = result
            interfaces_data_map[hostname] = parsed_data
            if hostname in device_map:
                devices_com_sucesso.append(device_map[hostname])

    end_collection_time = time.time()
    print(f"\n--- [INFO] Coleta SSH concluída em {end_collection_time - start_time:.2f} segundos ---")
    
    # 5. Salvar no banco OU Exibir no Console (LÓGICA DA NOVA FLAG)
    if not interfaces_data_map:
        print("[INFO] Nenhum dado de interface foi coletado. Nada para processar.")
    
    elif SAVE_TO_DATABASE:
        print("\n[INFO] SAVE_TO_DATABASE=True. Iniciando sincronização com o banco...")
        await sync_database(devices_com_sucesso, interfaces_data_map)
    
    else:
        print(f"\n[INFO] SAVE_TO_DATABASE=False. Exibindo dados coletados (Total: {len(devices_com_sucesso)} devices).")
        print("--- [DADOS DE INTERFACES COLETADOS (JSON/Dict)] ---")
        
        # Usamos pprint para uma saída formatada e legível
        pprint.pprint(interfaces_data_map)
        
        print("\n[INFO] Mude a variável 'SAVE_TO_DATABASE' para 'True' no topo do script para salvar estes dados.")

    end_total_time = time.time()
    print(f"\n--- [INFO] Sincronização TOTAL concluída em {end_total_time - start_time:.2f} segundos ---")


if __name__ == "__main__":
    asyncio.run(main())