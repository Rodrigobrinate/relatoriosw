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
        
        start_time = time.time() # Timeout para o loop de leitura
        
        # 4. Loop de leitura ATÉ VER O PROMPT
        while True:
            try:
                if channel.recv_ready():
                    chunk = channel.recv(65535)
                    if not chunk:
                        break # Canal fechou
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
                    
                time.sleep(0.2) # Não sobrecarregar a CPU
                
            except socket.timeout:
                 # Deu timeout de leitura (recv), mas o canal pode estar vivo
                 # A verificação de start_time vai pegar o timeout real
                pass
        
        # 5. Agora que lemos tudo, enviamos o QUIT
        channel.send('quit\n')
        time.sleep(0.5)
        client.close()
        
        # 6. Limpeza da Saída
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
        print(f"     [ERRO-SSH] Erro durante a execução do comando SSH em {host}: {e}")
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
        
        # O group(4) vai pegar '   LACP...' ou ''.
        # O .strip() limpa ambos os casos perfeitamente.
        descricao = match.group(4).strip()
        
        dados_finais.append({
            "interface_name": interface,
            "physical_status": status_fisico,
            "protocol_status": status_admin,
            "description": descricao
        })

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
            
            # 1. A chave de busca é o 'librenms_device_id' (para corrigir o erro UNIQUE)
            device = await db.device.upsert(
                where={'librenms_device_id': dev['device_id']}, 
                data={
                    'create': {
                        'librenms_device_id': dev['device_id'],
                        'hostname': dev['sysName'], # <-- USA 'sysName' AQUI
                        'ip_address': dev['ip'],
                        'os': dev.get('os'),
                        'vendor': dev.get('vendor')
                    },
                    'update': {
                        'hostname': dev['sysName'], # <-- E USA 'sysName' AQUI
                        'ip_address': dev['ip'],
                        'os': dev.get('os'),
                        'vendor': dev.get('vendor')
                    }
                }
            )
            
            # --- PASSO B: Sincronizar Interfaces ---
            
            # 2. 'dev['hostname']' é usado apenas como a CHAVE 
            #    para encontrar os dados no 'interfaces_data_map'
            #    (porque foi a chave que a função 'main' usou para criar o mapa)
            api_hostname_key = dev['hostname'] 
            if api_hostname_key not in interfaces_data_map:
                continue
            
            # 3. Usamos o nome bonito 'device.hostname' (que veio de sysName) no log
            print(f"   [DB] Sincronizando interfaces para {device.hostname} (DB ID: {device.id})...")
            
            interfaces = interfaces_data_map[api_hostname_key]
            if not interfaces:
                continue
                
            for iface in interfaces:
                # Esta parte já estava correta
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
# --- 6. ORQUESTRAÇÃO (NOVA VERSÃO - PARALELA) ---

async def process_device_interfaces(dev: dict, semaphore: asyncio.Semaphore) -> tuple | None:
    """
    Worker para coletar e parsear interfaces de UM dispositivo.
    Executa a coleta SSH (blocking) em uma thread separada.
    Retorna uma tupla (hostname, parsed_data) ou None.
    """
    
    # 'async with semaphore' garante que apenas X tarefas executem ao mesmo tempo
    async with semaphore:
        host = dev['ip'] 
        hostname = dev['hostname']
        print(f"\n--- [COLETA] Iniciando: {hostname} (IP: {host}) ---")
        
        raw_output = None
        try:
            # 1. Executa a função SÍNCRONA (blocking) em uma thread
            raw_output = await asyncio.to_thread(
                get_ssh_output, 
                host, 
                SSH_USERNAME, 
                SSH_PASSWORD, 
                SSH_COMMAND
            )
        except Exception as e:
            print(f"     [ERRO-THREAD] Erro ao executar get_ssh_output na thread para {hostname}: {e}")
            return None # Falha na coleta

        # 2. Processa a saída
        if raw_output:
            parsed_data = parse_output(raw_output)
            if parsed_data:
                print(f"   [COLETA] Sucesso! Parseadas {len(parsed_data)} interfaces de {hostname}.")
                return (hostname, parsed_data) # Retorna dados
            else:
                print(f"   [COLETA] Comando executado em {hostname}, mas nenhum dado de interface foi parseado.")
                return (hostname, []) # Retorna lista vazia
        else:
            print(f"   [COLETA] Não foi possível coletar dados de {hostname}.")
            return None # Falha na coleta


async def main():
    # ======================================================================
    # AQUI VOCÊ CONTROLA A SIMULTANEIDADE
    # Quantas conexões SSH podem ser abertas ao mesmo tempo.
    MAX_CONCURRENT_TASKS = 30
    # ======================================================================
    
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
    start_time = time.time() # Medir tempo total
    
    print(f"[INFO] Iniciando script de inventário (Paralelismo: {MAX_CONCURRENT_TASKS} devices)")

    # 1. Buscar dispositivos no LibreNMS (Síncrono, mas em thread)
    print("[INFO] Buscando dispositivos do LibreNMS (em thread)...")
    try:
        # Roda a função síncrona 'get_huawei_devices' (que usa 'requests')
        # em uma thread para não bloquear o loop asyncio.
        devices = await asyncio.to_thread(get_huawei_devices)
    except Exception as e:
        print(f"[ERRO FATAL] Falha ao buscar dispositivos do LibreNMS: {e}")
        return
        
    if not devices:
        print("[INFO] Nenhum dispositivo encontrado. Encerrando.")
        return

    print(f"[INFO] {len(devices)} dispositivos encontrados. Iniciando {len(devices)} tarefas de coleta SSH...")

    # 2. Criar lista de tarefas de coleta (uma para cada device)
    tasks = []
    for dev in devices:
        tasks.append(process_device_interfaces(dev, semaphore))

    # 3. Executar todas as tarefas de coleta simultaneamente
    # O 'gather' vai rodar até que todas as tarefas na lista sejam concluídas.
    # O Semáforo vai garantir que apenas MAX_CONCURRENT_TASKS rodem de fato.
    results = await asyncio.gather(*tasks)

    # 4. Processar os resultados da coleta
    interfaces_data_map = {}
    devices_com_sucesso = []
    
    # Criamos um map (hostname -> device_data) para facilitar a busca
    device_map = {dev['hostname']: dev for dev in devices}

    for result in results:
        # 'result' é a tupla (hostname, parsed_data) ou None
        if result:
            hostname, parsed_data = result
            interfaces_data_map[hostname] = parsed_data
            # Adiciona o device original à lista de sucesso
            if hostname in device_map:
                devices_com_sucesso.append(device_map[hostname])

    end_collection_time = time.time()
    print(f"\n--- [INFO] Coleta SSH concluída em {end_collection_time - start_time:.2f} segundos ---")
    
    # 5. Salvar tudo no banco de dados (Assíncrono)
    # Passamos APENAS os devices que tiveram coleta (para o sync_db não se perder)
    if interfaces_data_map:
        await sync_database(devices_com_sucesso, interfaces_data_map)
    else:
        print("[INFO] Nenhum dado de interface foi coletado. Nada para salvar no banco.")

    end_total_time = time.time()
    print(f"\n--- [INFO] Sincronização TOTAL concluída em {end_total_time - start_time:.2f} segundos ---")


if __name__ == "__main__":
    # Inicia o loop de eventos asyncio para rodar a função main
    asyncio.run(main())