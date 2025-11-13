#!/usr/bin/env python3
import paramiko
import time
import socket
import re
import asyncio
import pprint
from prisma import Prisma
from prisma.models import Device, NetworkInterface, TransceiverModule, TransceiverReading

# --- 1. CONFIGURAÇÕES ---

# !!! FLAG DE CONTROLE (PADRÃO: False) !!!
# False: Imprime os dados coletados no console (em JSON/dict).
# True: Salva os dados no banco de dados.
SAVE_TO_DATABASE = True

SSH_USERNAME = "zabbix.view"
SSH_PASSWORD = "view@123"



# --- 2. LÓGICA DO SSH (REUTILIZADA - FUNCIONA) ---
def get_ssh_output(host, username, password, command):
    """
    Conecta via SSH e usa 'exec_command' para rodar um único comando.
    """
    
    full_command = f"{command} | no-more"
    
    print(f"   -> [SSH] Executando em {host}: '{full_command[:45]}...'")
    output = ""
    error_output = ""
    
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
        COMMAND_TIMEOUT = 180.0 
        
        print(f"     [SSH-WAIT] Aguardando comando (Timeout global: {COMMAND_TIMEOUT}s)...")
        stdin, stdout, stderr = client.exec_command(full_command, timeout=COMMAND_TIMEOUT)
        
        output = stdout.read().decode('latin-1')
        error_output = stderr.read().decode('latin-1')
        
        client.close()
        
        if error_output:
            print(f"     [WARN-SSH] {host} retornou um erro (stderr): {error_output[:100]}")
            if "not found" in error_output or "error:" in error_output:
                return None 
                
        if len(output.splitlines()) <= 3: 
            print(f"     [WARN-SSH] Recebida resposta muito curta de {host} ({len(output.splitlines())} linhas).")
            return "" # Retorna vazio
        
        print(f"     [SSH-OK] Coleta de {host} concluída com sucesso.")
        return output

    except socket.timeout:
        print(f"     [ERRO-SSH] Timeout GLOBAL de {COMMAND_TIMEOUT}s atingido em {host}. O comando travou.")
        if 'client' in locals() and client: client.close()
        return None
    except Exception as e:
        print(f"     [ERRO-SSH] Erro durante a execução do comando SSH em {host}: {e}")
        if 'client' in locals() and client: client.close()
        return None

# --- 3. LÓGICA DE PARSING (NOVO PARSER ÓPTICO) ---

def _normalize_interface_name(name: str) -> str:
    """Normaliza o nome da interface."""
    return name.strip().replace(",", "")

def _get_juniper_float(key_regex: str, text: str, target_unit_regex: str) -> float | None:
    """
    Helper para extrair um float de uma linha do Juniper,
    priorizando a unidade alvo (ex: 'dBm' em vez de 'mW').
    """
    try:
        line_match = re.search(r"^\s*" + key_regex + r"\s*:\s*(.*)$", text, re.MULTILINE)
        if not line_match:
            return None
        
        content = line_match.group(1)
        
        # Tenta encontrar o valor com a unidade alvo (ex: 2.29 dBm)
        target_match = re.search(r"(-?[\d\.]+)\s*" + target_unit_regex, content)
        if target_match:
            return float(target_match.group(1))
        
        # Se não achar, pega o primeiro float que aparecer (ex: 37 degrees C)
        first_float_match = re.search(r"(-?[\d\.]+)", content)
        if first_float_match:
            return float(first_float_match.group(1))
            
    except (ValueError, TypeError, AttributeError):
        pass
    return None

def parse_optics_output(global_output_text: str) -> dict:
    """
    Analisa a saída completa de 'show interfaces diagnostics optics' e 
    retorna um dicionário de dados (module, reading) por interface.
    
    Baseado no arquivo 'show-interfaces-diagnostics-optics.txt'
    """
    all_data = {}
    
    # Divide a saída inteira em blocos, um para cada interface
    interface_blocks = re.split(r"^\s*Physical interface:\s*", global_output_text, flags=re.MULTILINE)
    
    if len(interface_blocks) <= 1:
        print("     [PARSE-GLOBAL] Nenhuma 'Physical interface' encontrada na saída.")
        return {}

    print(f"     [PARSE-GLOBAL] Encontrados {len(interface_blocks) - 1} blocos de 'Physical interface' na saída.")
    
    for block in interface_blocks[1:]: # Pula o primeiro item (que é vazio)
        try:
            name_match = re.match(r"([A-Za-z0-9-./]+)", block)
            if not name_match:
                continue
                
            interface_name = _normalize_interface_name(name_match.group(1))
            
            module_data = {}
            reading_data = {}

            # --- 1. DADOS DINÂMICOS (Reading) ---
            # Estes dados estão no "topo" do bloco
            reading_data['temperature'] = _get_juniper_float(r"Module temperature", block, r"degrees C")
            reading_data['voltage'] = _get_juniper_float(r"Module voltage", block, r"V")
            
            # Procura pelo bloco "Lane 0" para dados de Rx/Tx/Bias
            lane_text_block = block # Por padrão, usa o bloco todo
            lane_0_match = re.search(r"^\s*Lane 0([\s\S]*?)(?=^\s*Lane 1|^\s*$)", block, re.MULTILINE)
            if lane_0_match:
                lane_text_block = lane_0_match.group(1)
                
            reading_data['bias_current'] = _get_juniper_float(r"Laser bias current", lane_text_block, r"mA")
            reading_data['tx_power'] = _get_juniper_float(r"Laser output power", lane_text_block, r"dBm")
            reading_data['rx_power'] = _get_juniper_float(r"Laser receiver power", lane_text_block, r"dBm")

            # --- 2. DADOS ESTÁTICOS (Module - Thresholds) ---
            # Esses dados também estão no "topo" do bloco
            module_data['temp_high'] = _get_juniper_float(r"Module temperature high alarm threshold", block, r"degrees C")
            module_data['temp_low'] = _get_juniper_float(r"Module temperature low alarm threshold", block, r"degrees C")
            module_data['volt_high'] = _get_juniper_float(r"Module voltage high alarm threshold", block, r"V")
            module_data['volt_low'] = _get_juniper_float(r"Module voltage low alarm threshold", block, r"V")
            module_data['bias_high'] = _get_juniper_float(r"Laser bias current high alarm threshold", block, r"mA")
            module_data['bias_low'] = _get_juniper_float(r"Laser bias current low alarm threshold", block, r"mA")
            
            # O parser _get_juniper_float vai priorizar o valor em 'dBm'
            module_data['tx_power_high'] = _get_juniper_float(r"Laser output power high alarm threshold", block, r"dBm")
            module_data['tx_power_low'] = _get_juniper_float(r"Laser output power low alarm threshold", block, r"dBm")
            module_data['rx_power_high'] = _get_juniper_float(r"Laser rx power high alarm threshold", block, r"dBm")
            module_data['rx_power_low'] = _get_juniper_float(r"Laser rx power low alarm threshold", block, r"dBm")
            
            # Limiares de Aviso (Warnings)
            module_data['rx_power_high_warning'] = _get_juniper_float(r"Laser rx power high warning threshold", block, r"dBm")
            module_data['rx_power_low_warning'] = _get_juniper_float(r"Laser rx power low warning threshold", block, r"dBm")
            module_data['tx_power_high_warning'] = _get_juniper_float(r"Laser output power high warning threshold", block, r"dBm")
            module_data['tx_power_low_warning'] = _get_juniper_float(r"Laser output power low warning threshold", block, r"dBm")

            # Define o status como 'present' se achamos dados
            if reading_data['temperature'] or reading_data['rx_power']:
                reading_data['transceiver_status'] = "present"
            else:
                 reading_data['transceiver_status'] = "no_diag" # Ou 'absent', mas esse comando só retorna dados se houver algo

            # Limpa valores nulos (None) dos dicionários
            clean_module_data = {k: v for k, v in module_data.items() if v is not None}
            clean_reading_data = {k: v for k, v in reading_data.items() if v is not None}
            
            # Só adiciona se tivermos dados de leitura
            if clean_reading_data:
                all_data[interface_name] = {
                    "module": clean_module_data, 
                    "reading": clean_reading_data
                }
            
        except Exception as e:
            print(f"     [ERRO-PARSE] Erro fatal ao processar bloco para {interface_name}: {e}")

    print(f"\n     [PARSE-GLOBAL] Análise concluída. {len(all_data)} interfaces com dados ópticos extraídos.")
    return all_data


# --- 4. ORQUESTRAÇÃO (MAIN) ---

async def process_device_monitoring(db: Prisma, dev: Device, semaphore: asyncio.Semaphore):
    """
    Processa um UNICO dispositivo: coleta, parseia e decide se salva ou printa.
    """
    
    # --- NOVO COMANDO ---
    COMMAND = "show interfaces diagnostics optics"
    
    async with semaphore:
        print(f"\n--- [DEV] Iniciando Processamento Óptico: {dev.hostname} (IP: {dev.ip_address}) ---")

        try:
            # Pega as interfaces e o ÚLTIMO módulo salvo para comparar S/N
            db_interfaces = await db.networkinterface.find_many(
                where={'device_id': dev.id},
                include={
                    'modules': {
                        'order_by': {'timestamp': 'desc'},
                        'take': 1
                    }
                }
            )
            if not db_interfaces:
                print(f"   [INFO] Nenhuma interface encontrada para {dev.hostname} no DB. Pulando dispositivo.")
                return
            print(f"   [INFO] Encontradas {len(db_interfaces)} interfaces no DB. Buscando dados ópticos...")
        except Exception as e:
            print(f"   [ERRO-DB] Falha ao buscar interfaces de {dev.hostname}: {e}")
            return

        raw_output = None
        try:
            raw_output = await asyncio.to_thread(
                get_ssh_output, 
                dev.ip_address, 
                SSH_USERNAME, 
                SSH_PASSWORD, 
                COMMAND
            )
        except Exception as e:
            print(f"   [ERRO-THREAD] Erro ao executar get_ssh_output na thread para {dev.hostname}: {e}")
            return
        
        if not raw_output:
            print(f"   [ERRO-SSH] Falha ao obter dados ópticos de {dev.ip_address} (saída vazia ou falha na conexão). Pulando dispositivo.")
            return

        try:
            all_parsed_data = parse_optics_output(raw_output)
        except Exception as e:
            print(f"   [ERRO-PARSE] Falha ao analisar dados ópticos de {dev.hostname}: {e}")
            return

        if not all_parsed_data:
            print(f"   [WARN] Parser não encontrou dados ópticos na saída de {dev.ip_address}.")
            return
            
        
        # --- PARTE 4: Salvar no DB ou Printar no Console ---
        
        readings_salvas = 0
        modules_salvos = 0
        
        # Dados para printar se SAVE_TO_DATABASE == False
        print_output_data = {}
        
        db_interface_map = {iface.interface_name: iface for iface in db_interfaces}

        for iface_name, parsed_data in all_parsed_data.items():
            
            # Só processa interfaces que monitoramos no DB
            if iface_name not in db_interface_map:
                continue
                
            iface = db_interface_map[iface_name]
            
            # Extrai os dados parseados
            module_data = parsed_data.get('module', {})
            reading_data = parsed_data.get('reading', {})
            
            if SAVE_TO_DATABASE:
                # --- LÓGICA DE BANCO DE DADOS ATIVADA ---
                
                # 4a. Salva TransceiverReading (Temp, Rx, Tx)
                try:
                    if reading_data.get('transceiver_status'): # Só salva se tiver status
                        reading_data['interface_id'] = iface.id
                        await db.transceiverreading.create(data=reading_data)
                        readings_salvas += 1
                except Exception as e:
                    print(f"     [ERRO-DB-READ] Falha ao salvar leitura de {iface.interface_name}: {e}")
                
                # 4b. Salva TransceiverModule (S/N e Thresholds) - CONDICIONAL
                # Nota: este comando NÃO pega S/N, então o S/N será 'None'
                try:
                    last_module = iface.modules[0] if iface.modules else None
                    last_serial = last_module.serial_number if last_module and last_module.serial_number else None
                    new_serial = module_data.get('serial_number') # Será 'None'

                    if new_serial != last_serial:
                        print(f"     [SAVE-MODULE] *** Detecção de mudança de módulo em {iface.interface_name}! ***")
                        print(f"       S/N Antigo: {last_serial} -> S/N Novo: {new_serial}")
                        
                        module_data['interface_id'] = iface.id
                        await db.transceivermodule.create(data=module_data)
                        modules_salvos += 1
                except Exception as e:
                    print(f"     [ERRO-DB-MODULE] Falha ao salvar módulo de {iface.interface_name}: {e}")
            
            else:
                # --- LÓGICA DE PRINT NO CONSOLE ATIVADA ---
                print_output_data[iface_name] = parsed_data

        # --- Resumo por dispositivo ---
        if SAVE_TO_DATABASE:
            print(f"\n--- [DEV] Concluído Processamento Óptico de {dev.hostname} (Modo: Salvar) ---")
            print(f"   - {readings_salvas} novas leituras de transceiver (Temp/Rx/Tx) salvas.")
            print(f"   - {modules_salvos} novos eventos de módulo (Thresholds) registrados.")
        else:
            # Imprime o dicionário completo UMA VEZ por dispositivo
            print(f"\n--- [DEV] Concluído Processamento Óptico de {dev.hostname} (Modo: Console) ---")
            pprint.pprint(print_output_data)


async def main():
    db = Prisma()
    
    MAX_CONCURRENT_TASKS = 20 
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
    
    print("[INFO] Iniciando script de DADOS ÓPTICOS (Juniper)...")
    if SAVE_TO_DATABASE:
        print("[INFO] Modo: SALVAR NO BANCO DE DADOS")
    else:
        print("[INFO] Modo: EXIBIR NO CONSOLE (Nenhum dado será salvo)")
    print(f"[INFO] Limite de {MAX_CONCURRENT_TASKS} coletas simultâneas.")
    
    start_total_time = time.time()
    
    try:
        print("[DB] Conectando ao banco de dados...")
        await db.connect()
        print("[DB] Conexão estabelecida.")
        
        print("[DB] Buscando lista de dispositivos Juniper...")
        devices = await db.device.find_many(
             where={'os': 'junos'}
        )
        
        if not devices:
            print("[ERRO] Nenhum dispositivo com 'os' == 'junos' encontrado no banco.")
            return

        print(f"[INFO] Encontrados {len(devices)} dispositivos. Criando tarefas...")
        
        tasks = []
        for dev in devices:
            tasks.append(process_device_monitoring(db, dev, semaphore))
        
        await asyncio.gather(*tasks)
        
        end_total_time = time.time()
        
        print("\n==============================================")
        print(f"[INFO] Coleta ÓPTICA concluída para todos os {len(devices)} dispositivos.")
        print(f"[INFO] Tempo total de execução: {end_total_time - start_total_time:.2f} segundos.")
        
    except Exception as e:
        print(f"\n[ERRO FATAL] Ocorreu um erro: {e}")
    finally:
        if db.is_connected():
            await db.disconnect()
            print("[INFO] Desconectado do banco de dados.")

if __name__ == "__main__":
    asyncio.run(main())