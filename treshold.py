#!/usr/bin/env python3
import paramiko
import time
import socket
import re
import asyncio
from prisma import Prisma
# Importe os novos modelos
from prisma.models import Device, NetworkInterface, TransceiverModule, TransceiverReading

# --- 1. CONFIGURAÇÕES ---
SSH_USERNAME = "zabbix.view"
SSH_PASSWORD = "view@123"

# --- 2. LÓGICA DO SSH (ROBUSTA) ---
def get_ssh_output(host, username, password, command):
    """
    Conecta via SSH, executa um comando lendo até o prompt,
    e só então envia 'quit'.
    """
    print(f"   -> [SSH] Executando em {host}: '{command[:35]}...'")
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
                print(f"     [ERRO-SSH] Timeout de 20s atingido esperando o comando em {host}")
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
        print(f"     [ERRO-SSH] Erro durante a execução do comando SSH em {host}: {e}")
        if 'client' in locals() and client: client.close()
        return None

# --- 3. LÓGICA DE PARSING (AJUSTADA PARA SAÍDA GLOBAL) ---

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

def _parse_single_interface_block(output_text: str) -> dict | None:
    """
    Analisa um *único bloco* de texto e retorna um dicionário
    separado em 'module_data' (estático) e 'reading_data' (dinâmico).
    """
    
    # Dicionários para os novos modelos
    module_data = {}
    reading_data = {}

    # --- Nível 1: Verificar status principal ---
    if "transceiver is absent" in output_text:
        reading_data['transceiver_status'] = "absent"
        print("       [PARSE-BLOCK] Status detectado: absent")
        
    elif "does not support diagnostic" in output_text:
        reading_data['transceiver_status'] = "no_diag"
        print("       [PARSE-BLOCK] Status detectado: no_diag")
        
    elif "This interface does not support transceiver" in output_text:
        return None # Ignora interfaces lógicas
    else:
        reading_data['transceiver_status'] = "present"
        print("       [PARSE-BLOCK] Status detectado: present (com diagnóstico)")

    # --- Nível 2: Funções auxiliares de Regex (ancoradas) ---
    def get_string(key_regex):
        match = re.search(r"^\s+" + key_regex, output_text, re.MULTILINE)
        if match:
            value = match.group(1).strip()
            return value if value != '-' else None
        return None

    def get_float(key_regex):
        match = re.search(r"^\s+" + key_regex, output_text, re.MULTILINE)
        if not match: return None
        try:
            val_match = re.search(r"(-?[\d.]+)", match.group(1))
            return float(val_match.group(1)) if val_match else None
        except (ValueError, TypeError, AttributeError):
            return None
            
    def get_multilane_string(key_regex, stop_keyword):
        regex = rf"^\s+{key_regex}\s*:\s*([\s\S]*?)(?=^\s+{stop_keyword}|^\s+-{{3,}})"
        match = re.search(regex, output_text, re.MULTILINE)
        if match:
            return ' '.join(match.group(1).split())
        return None

    # ***** INÍCIO DA NOVA FUNÇÃO DE CORREÇÃO *****
    def get_first_float_from_string(raw_string: str | None) -> float | None:
        """
        Tenta extrair o *primeiro* valor float de uma string.
        Funciona para '33.50' e para '7.10|7.10(Lane0|Lane1)'.
        """
        if raw_string is None:
            return None
        
        # Tenta encontrar o primeiro número (com decimal ou sinal negativo)
        match = re.search(r"(-?[\d.]+)", raw_string)
        if not match:
            return None
            
        try:
            return float(match.group(1))
        except (ValueError, TypeError):
            return None
    # ***** FIM DA NOVA FUNÇÃO DE CORREÇÃO *****


    # --- Nível 3: Mapear e Extrair Dados ---
    
    # Dados do Módulo (Estáticos)
    module_data['transceiver_type'] = get_string(r"Transceiver Type\s*:\s*(.*)")
    module_data['connector_type'] = get_string(r"Connector Type\s*:\s*(.*)")
    module_data['wavelength_nm'] = get_string(r"Wavelength\(nm\)\s*:\s*(.*)")
    module_data['transfer_distance_m'] = get_string(r"Transfer Distance\(m\)\s*:\s*(.*)")
    module_data['vendor_part_number'] = get_string(r"Vendor Part Number\s*:\s*(.*)")
    module_data['serial_number'] = get_string(r"Manu\. Serial Number\s*:\s*(.*)")
    module_data['manufacturing_date'] = get_string(r"Manufacturing Date\s*:\s*(.*)")
    
    common_info_match = re.search(r"Common information:([\s\S]*?)(?:Manufacture information:|-{3,})", output_text, re.MULTILINE)
    if common_info_match:
        vendor_match = re.search(r"^\s+Vendor Name\s*:\s*(.*)", common_info_match.group(1), re.MULTILINE)
        if vendor_match:
            module_data['vendor_name'] = vendor_match.group(1).strip()
            
    # Dados de Leitura (Dinâmicos) e Thresholds (Estáticos)
    if reading_data['transceiver_status'] == "present":
        # Leituras (Dinâmicas)
        reading_data['temperature'] = get_float(r"Temperature\(\S+\)\s*:\s*(.*)")
        reading_data['voltage'] = get_float(r"Voltage\(V\)\s*:\s*(.*)")
        
        # ***** INÍCIO DA CORREÇÃO DE TIPO (String -> Float) *****
        # Obtém a string bruta (que pode ser multi-lane)
        bias_str = get_multilane_string(r"Bias Current\(mA\)", r"Bias High Threshold")
        rx_str = get_multilane_string(r"RX Power\(dBM\)", r"RX Power High Warning")
        tx_str = get_multilane_string(r"TX Power\(dBM\)", r"TX Power High Warning")

        # Converte para float pegando apenas o primeiro valor
        reading_data['bias_current'] = get_first_float_from_string(bias_str)
        reading_data['rx_power'] = get_first_float_from_string(rx_str)
        reading_data['tx_power'] = get_first_float_from_string(tx_str)
        # ***** FIM DA CORREÇÃO DE TIPO *****
        
        # Thresholds (Estáticos, parte do Módulo)
        module_data['temp_high'] = get_float(r"Temp High Threshold\(\S+\)\s*:\s*(.*)")
        module_data['temp_low'] = get_float(r"Temp Low\s+Threshold\(\S+\)\s*:\s*(.*)")
        module_data['volt_high'] = get_float(r"Volt High Threshold\(V\)\s*:\s*(.*)")
        module_data['volt_low'] = get_float(r"Volt Low\s+Threshold\(V\)\s*:\s*(.*)")
        module_data['bias_high'] = get_float(r"Bias High Threshold\(mA\)\s*:\s*(.*)")
        module_data['bias_low'] = get_float(r"Bias Low\s+Threshold\(mA\)\s*:\s*(.*)")
        module_data['rx_power_high'] = get_float(r"RX Power High Threshold\(dBM\)\s*:\s*(.*)")
        module_data['rx_power_low'] = get_float(r"RX Power Low\s+Threshold\(dBM\)\s*:\s*(.*)")
        module_data['tx_power_high'] = get_float(r"TX Power High Threshold\(dBM\)\s*:\s*(.*)")
        module_data['tx_power_low'] = get_float(r"TX Power Low\s+Threshold\(dBM\)\s*:\s*(.*)")
        module_data['rx_power_high_warning'] = get_float(r"RX Power High Warning\(dBM\)\s*:\s*(.*)")
        module_data['rx_power_low_warning'] = get_float(r"RX Power Low\s+Warning\(dBM\)\s*:\s*(.*)")
        module_data['tx_power_high_warning'] = get_float(r"TX Power High Warning\(dBM\)\s*:\s*(.*)")
        module_data['tx_power_low_warning'] = get_float(r"TX Power Low\s+Warning\(dBM\)\s*:\s*(.*)")

    # Limpa valores nulos (None) dos dicionários
    clean_module_data = {k: v for k, v in module_data.items() if v is not None}
    clean_reading_data = {k: v for k, v in reading_data.items() if v is not None}

    print(f"       [PARSE-MOD] Dados Módulo: {clean_module_data}")
    print(f"       [PARSE-READ] Dados Leitura: {clean_reading_data}")

    return {
        "module": clean_module_data,
        "reading": clean_reading_data
    }

def parse_global_verbose_output(global_output_text: str) -> dict:
    """
    Analisa a saída completa e retorna um dicionário de dicionários
    (module_data e reading_data) por interface.
    """
    all_data = {}
    
    # --- PASSO 1: Encontrar e processar blocos de transceivers PRESENTES ---
    # CORREÇÃO: Permite nomes que começam com números (ex: 100GE, 40GE)
    interface_header_regex = r"^([A-Za-z0-9-/.]+) transceiver information:"
    
    headers = list(re.finditer(interface_header_regex, global_output_text, re.MULTILINE))
    
    if headers:
        print(f"     [PARSE-GLOBAL] Encontrados {len(headers)} blocos de interface na saída.")
        for i, header_match in enumerate(headers):
            try:
                long_name = header_match.group(1)
                interface_name = _normalize_interface_name(long_name)
                
                start_index = header_match.start()
                end_index = headers[i + 1].start() if (i + 1) < len(headers) else len(global_output_text)
                interface_block_text = global_output_text[start_index:end_index]
                
                print(f"\n     --- [IFACE-PARSE] Processando Bloco para: {long_name} (como {interface_name}) ---")
                
                # parsed_data agora é {'module': {...}, 'reading': {...}}
                parsed_data = _parse_single_interface_block(interface_block_text) 
                
                if parsed_data:
                    all_data[interface_name] = parsed_data
                else:
                    print(f"       [PARSE-BLOCK] Interface {long_name} ({interface_name}) ignorada.")
            except Exception as e:
                print(f"     [ERRO-PARSE] Erro ao processar bloco para {long_name}: {e}")

    # --- PASSO 2: Encontrar e processar portas 'absent' ---
    absent_regex = r"Info: Port ([A-Za-z-]+\d+[\d/.]+), transceiver is absent\."
    absent_interfaces_long = re.findall(absent_regex, global_output_text)
    
    if absent_interfaces_long:
        print(f"     [PARSE-GLOBAL] Encontradas {len(absent_interfaces_long)} interfaces 'absent'.")
        for long_name in absent_interfaces_long:
            iface_name = _normalize_interface_name(long_name) 
            if iface_name not in all_data:
                print(f"     [PARSE-GLOBAL] Marcando {long_name} (como {iface_name}) como 'absent'.")
                all_data[iface_name] = {
                    "module": {"serial_number": None}, # Serial None é crucial para a lógica
                    "reading": {"transceiver_status": "absent"}
                }
    
    if not headers and not absent_interfaces_long:
         print("     [PARSE-GLOBAL] Nenhuma interface (presente ou ausente) encontrada na saída.")
         return {}

    print(f"\n     [PARSE-GLOBAL] Análise concluída. {len(all_data)} interfaces com dados extraídos.")
    return all_data


# --- 4. ORQUESTRAÇÃO (MAIN) ---

async def main():
    db = Prisma()
    
    print("[INFO] Iniciando script de coleta VERBOSA de transceiver...")
    
    try:
        print("[DB] Conectando ao banco de dados...")
        await db.connect()
        print("[DB] Conexão estabelecida.")
        
        print("[DB] Buscando lista de dispositivos...")
        devices = await db.device.find_many()
        
        if not devices:
            print("[ERRO] Nenhum dispositivo encontrado no banco de dados. Execute 'relatorio.py' primeiro.")
            return

        print(f"[INFO] Encontrados {len(devices)} dispositivos no banco de dados para verificar.")
        
        COMMAND_GLOBAL = "display transceiver verbose"
        
        for dev in devices:
            print(f"\n--- [DEV] Processando Dispositivo: {dev.hostname} (IP: {dev.ip_address}) ---")
            
            # Incluir as relações na busca
            db_interfaces = await db.networkinterface.find_many(
                where={'device_id': dev.id},
                include={
                    'modules': { # Pega apenas o último módulo registrado
                        'order_by': {'timestamp': 'desc'},
                        'take': 1
                    }
                }
            )
            
            if not db_interfaces:
                print(f"   [INFO] Nenhuma interface encontrada para {dev.hostname} no DB. Pulando dispositivo.")
                continue
                
            print(f"   [INFO] Encontradas {len(db_interfaces)} interfaces no DB. Buscando dados de transceiver...")
            
            global_raw_output = get_ssh_output(dev.ip_address, SSH_USERNAME, SSH_PASSWORD, COMMAND_GLOBAL)
            
            if not global_raw_output or "Error:" in global_raw_output:
                print(f"   [ERRO-SSH] Falha ao obter dados de {dev.ip_address} ou comando retornou erro. Pulando dispositivo.")
                continue
            
            all_transceiver_data = parse_global_verbose_output(global_raw_output)
            
            if not all_transceiver_data:
                print(f"   [WARN] Parser não encontrou dados de transceiver na saída de {dev.ip_address}.")
                continue
            
            readings_salvas = 0
            modules_salvos = 0
            interfaces_sem_dados = 0
            
            for iface in db_interfaces:
                
                if iface.interface_name in all_transceiver_data:
                    
                    parsed_data = all_transceiver_data[iface.interface_name]
                    module_data = parsed_data.get('module', {})
                    reading_data = parsed_data.get('reading', {})

                    # --- LÓGICA DE SALVAMENTO DE LEITURA (A CADA 5 MIN) ---
                    try:
                        # Adiciona o ID da interface e salva na tabela de Leituras
                        reading_data['interface_id'] = iface.id
                        await db.transceiverreading.create(data=reading_data)
                        readings_salvas += 1
                        print(f"     [SAVE-READ] Leitura de {iface.interface_name} salva.")
                    except Exception as e:
                        print(f"     [ERRO-DB-READ] Falha ao salvar leitura de {iface.interface_name}: {e}")

                    # --- LÓGICA DE SALVAMENTO DE MÓDULO (CONDICIONAL) ---
                    try:
                        # Compara S/N novo com o S/N antigo
                        last_module = iface.modules[0] if iface.modules else None
                        last_serial = last_module.serial_number if last_module else None
                        new_serial = module_data.get('serial_number') # Pode ser None se 'absent'

                        if new_serial != last_serial:
                            print(f"     [SAVE-MODULE] *** Detecção de mudança de módulo em {iface.interface_name}! ***")
                            print(f"       S/N Antigo: {last_serial} -> S/N Novo: {new_serial}")
                            
                            # Adiciona o ID da interface e salva na tabela de Módulos
                            module_data['interface_id'] = iface.id
                            await db.transceivermodule.create(data=module_data)
                            modules_salvos += 1
                        
                    except Exception as e:
                        print(f"     [ERRO-DB-MODULE] Falha ao salvar módulo de {iface.interface_name}: {e}")

                else:
                    interfaces_sem_dados += 1

            print(f"\n--- [DEV] Concluído Processamento de {dev.hostname} ---")
            print(f"   - {readings_salvas} leituras dinâmicas salvas.")
            print(f"   - {modules_salvos} novos eventos de módulo registrados.")
            print(f"   - {interfaces_sem_dados} interfaces do DB não encontradas na saída (prov. lógicas/sem dados).")
        
        print("\n==============================================")
        print("[INFO] Coleta VERBOSA concluída para todos os dispositivos.")
        
    except Exception as e:
        print(f"\n[ERRO FATAL] Ocorreu um erro: {e}")
    finally:
        if db.is_connected():
            await db.disconnect()
            print("[INFO] Desconectado do banco de dados.")

if __name__ == "__main__":
    asyncio.run(main())