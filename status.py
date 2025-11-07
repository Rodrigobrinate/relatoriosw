#!/usr/bin/env python3
import paramiko
import time
import socket
import re
import asyncio
from prisma import Prisma
# Importe o novo modelo
from prisma.models import Device, NetworkInterface, InterfaceStats

# --- 1. CONFIGURAÇÕES ---
SSH_USERNAME = "zabbix.view"
SSH_PASSWORD = "view@123"

# --- 2. LÓGICA DO SSH (Reutilizada do seu outro script) ---
def get_ssh_output(host, username, password, command):
    """
    Conecta via SSH, executa um comando lendo até o prompt,
    e só então envia 'quit'.
    """
    print(f"  -> [SSH] Executando em {host}: '{command[:35]}...'")
    output = ""
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host, port=22, username=username, password=password,
            look_for_keys=False, allow_agent=False, banner_timeout=200, timeout=10
        )
    except Exception as e:
        print(f"    [ERRO-SSH] Falha ao conectar SSH em {host}: {e}")
        return None
    
    try:
        channel = client.invoke_shell()
        channel.settimeout(10.0) 

        time.sleep(1)
        try: channel.recv(65535)
        except socket.timeout: pass
        
        channel.send('screen-length 0 temporary\n')
        time.sleep(0.5)
        try: channel.recv(65535)
        except socket.timeout: pass
        
        channel.send(command + '\n')
        
        start_time = time.time()
        while True:
            if channel.recv_ready():
                chunk = channel.recv(65535)
                if not chunk: 
                    break 
                output += chunk.decode('latin-1')
                stripped_output = output.strip()
                if stripped_output.endswith('>') or stripped_output.endswith(']'):
                    break
            
            if channel.exit_status_ready():
                break

            if time.time() - start_time > 20.0:
                print(f"    [ERRO-SSH] Timeout de 20s atingido esperando o comando em {host}")
                break
                
            time.sleep(0.2)
            
        channel.send('quit\n')
        time.sleep(0.5)
        client.close()
        
        lines = output.splitlines()
        if len(lines) <= 2: return "" 
        clean_lines = [line for line in lines[1:-1] if line.strip()]
        return "\n".join(clean_lines)

    except Exception as e:
        print(f"    [ERRO-SSH] Erro durante a execução do comando SSH em {host}: {e}")
        if 'client' in locals() and client: client.close()
        return None

# --- 3. LÓGICA DE PARSING (NOVA) ---

def _normalize_interface_name(name: str) -> str:
    """
    Converte nomes longos de interface (XGigabitEthernet) para curtos (XGE)
    para bater com o nome que provavelmente está no banco de dados.
    """
    name = name.strip()
    if name.startswith("XGigabitEthernet"):
        return name.replace("XGigabitEthernet", "XGE", 1)
    if name.startswith("GigabitEthernet"):
        return name.replace("GigabitEthernet", "GE", 1)
    if name.startswith("Ethernet"):
        return name.replace("Ethernet", "Eth", 1)
    # Nomes como 100GE, 40GE, Eth-Trunk já são "curtos"
    return name

def parse_interface_brief(output_text: str) -> dict:
    """
    Analisa a saída do 'display interface brief' e retorna um dicionário
    com dados de estatísticas e status por interface.
    """
    all_data = {}
    lines = output_text.splitlines()

    for line in lines:
        line = line.strip()
        # Pula cabeçalho ou linhas vazias
        if not line or "Interface" in line or "PHY" in line or "InUti" in line:
            continue 

        parts = line.split()
        if len(parts) < 7:
            continue # Linha malformada

        try:
            # Pega as colunas da direita para a esquerda (mais seguro)
            out_errors = parts[-1]
            in_errors = parts[-2]
            out_uti = parts[-3]
            in_uti = parts[-4]
            protocol = parts[-5].replace('*', '') # Remove o '*' de *down
            phy = parts[-6].replace('*', '')      # Remove o '*' de *down
            
            # O nome da interface é todo o resto
            interface_name = " ".join(parts[:-6])
            
            # Normaliza o nome (ex: XGigabitEthernet0/0/3 -> XGE0/0/3)
            normalized_name = _normalize_interface_name(interface_name)

            # 1. Dados para a tabela de histórico 'InterfaceStats'
            stats_data = {
                'in_uti': float(in_uti.replace('%', '')),
                'out_uti': float(out_uti.replace('%', '')),
                'in_errors': int(in_errors),
                'out_errors': int(out_errors)
            }
            
            # 2. Dados para a tabela principal 'NetworkInterface'
            status_data = {
                'physical_status': phy,
                'protocol_status': protocol
            }

            all_data[normalized_name] = {
                "stats": stats_data,
                "status": status_data
            }
        except (ValueError, IndexError) as e:
            print(f"  [WARN-PARSE] Falha ao processar linha: '{line}'. Erro: {e}")

    return all_data


# --- 4. ORQUESTRAÇÃO (MAIN) ---

async def main():
    db = Prisma()
    
    print("[INFO] Iniciando script de coleta de ESTATÍSTICAS de interface...")
    
    try:
        print("[DB] Conectando ao banco de dados...")
        await db.connect()
        print("[DB] Conexão estabelecida.")
        
        print("[DB] Buscando lista de dispositivos...")
        devices = await db.device.find_many()
        
        if not devices:
            print("[ERRO] Nenhum dispositivo encontrado no banco de dados.")
            return

        print(f"[INFO] Encontrados {len(devices)} dispositivos no banco de dados para verificar.")
        
        COMMAND = "display interface brief"
        
        for dev in devices:
            print(f"\n--- [DEV] Processando Dispositivo: {dev.hostname} (IP: {dev.ip_address}) ---")
            
            # Busca todas as interfaces do DB *primeiro*
            db_interfaces = await db.networkinterface.find_many(
                where={'device_id': dev.id}
            )
            
            if not db_interfaces:
                print(f"  [INFO] Nenhuma interface encontrada para {dev.hostname} no DB. Pulando dispositivo.")
                continue
                
            print(f"  [INFO] Encontradas {len(db_interfaces)} interfaces no DB. Buscando estatísticas...")
            
            # 1. Executa o SSH *UMA VEZ* por dispositivo
            raw_output = get_ssh_output(dev.ip_address, SSH_USERNAME, SSH_PASSWORD, COMMAND)
            
            if not raw_output or "Error:" in raw_output:
                print(f"  [ERRO-SSH] Falha ao obter dados de {dev.ip_address} ou comando retornou erro. Pulando dispositivo.")
                continue
            
            # 2. Analisa (parse) a saída completa
            # Retorna: {"XGE0/0/1": {"stats": {...}, "status": {...}}, ...}
            parsed_data = parse_interface_brief(raw_output)
            
            if not parsed_data:
                print(f"  [WARN] Parser não encontrou dados na saída de {dev.ip_address}.")
                continue
            
            stats_salvas = 0
            status_atualizado = 0
            
            # 3. Itera pelas interfaces do DB para salvar os dados
            for iface in db_interfaces:
                
                # Verifica se o parser encontrou dados para esta interface
                if iface.interface_name in parsed_data:
                    
                    data = parsed_data[iface.interface_name]
                    stats_data = data['stats']
                    status_data = data['status']
                    
                    # 4. Salva o histórico na nova tabela 'InterfaceStats'
                    try:
                        stats_data['interface_id'] = iface.id
                        await db.interfacestats.create(data=stats_data)
                        stats_salvas += 1
                    except Exception as e:
                        print(f"    [ERRO-DB-STATS] Falha ao salvar stats de {iface.interface_name}: {e}")

                    # 5. Atualiza o status (up/down) na tabela 'NetworkInterface'
                    try:
                        await db.networkinterface.update(
                            where={'id': iface.id},
                            data=status_data
                        )
                        status_atualizado += 1
                    except Exception as e:
                        print(f"    [ERRO-DB-STATUS] Falha ao atualizar status de {iface.interface_name}: {e}")

            print(f"\n--- [DEV] Concluído Processamento de {dev.hostname} ---")
            print(f"  - {stats_salvas} novos registros de estatísticas salvos.")
            print(f"  - {status_atualizado} interfaces com status (up/down) atualizado.")
        
        print("\n==============================================")
        print("[INFO] Coleta de ESTATÍSTICAS concluída.")
        
    except Exception as e:
        print(f"\n[ERRO FATAL] Ocorreu um erro: {e}")
    finally:
        if db.is_connected():
            await db.disconnect()
            print("[INFO] Desconectado do banco de dados.")

if __name__ == "__main__":
    asyncio.run(main())