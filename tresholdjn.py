#!/usr/bin/env python3
import paramiko
import time
import socket
import re
import asyncio
import pprint
from prisma import Prisma
from prisma.models import Device, NetworkInterface, TransceiverModule, TransceiverReading, InterfaceStats

# --- 1. CONFIGURAÇÕES ---

# !!! FLAG DE CONTROLE (PADRÃO: False) !!!
# False: Imprime os dados coletados no console (em JSON/dict).
# True: Salva os dados no banco de dados.
SAVE_TO_DATABASE = True

SSH_USERNAME = "zabbix.view"
SSH_PASSWORD = "view@123"



# --- 2. LÓGICA DO SSH (NOVA VERSÃO - USANDO EXEC_COMMAND) ---
def get_ssh_output(host, username, password, command):
    """
    Conecta via SSH e usa 'exec_command' para rodar um único comando,
    que é a forma mais robusta de capturar saídas longas no Juniper.
    """
    
    # Anexa o comando '| no-more' para desabilitar a paginação no Junos
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
        # Define um timeout global para o comando.
        # 180 segundos (3 minutos) deve ser suficiente para 'extensive'.
        COMMAND_TIMEOUT = 180.0 
        
        print(f"     [SSH-WAIT] Aguardando comando (Timeout global: {COMMAND_TIMEOUT}s)...")
        stdin, stdout, stderr = client.exec_command(full_command, timeout=COMMAND_TIMEOUT)
        
        # Lê a saída padrão (os dados que queremos)
        output = stdout.read().decode('latin-1')
        
        # Lê a saída de erro (para debug)
        error_output = stderr.read().decode('latin-1')
        
        client.close()
        
        # --- DEBUG (LOGS QUE VOCÊ PEDIU) ---
        if error_output:
            print(f"     [WARN-SSH] {host} retornou um erro (stderr):")
            print("     [WARN-SSH] === INÍCIO DO STDERR ===")
            print(error_output)
            print("     [WARN-SSH] === FIM DO STDERR ===")
            # Se o erro for 'command not found' ou similar, paramos.
            if "not found" in error_output or "error:" in error_output:
                return None 
                
        if len(output.splitlines()) <= 5: 
            print(f"     [WARN-SSH] Recebida resposta muito curta de {host} ({len(output.splitlines())} linhas).")
            print("     [WARN-SSH] === INÍCIO DA SAÍDA DESCARTADA ===")
            print(output if output else "[NENHUMA SAÍDA RECEBIDA]")
            print("     [WARN-SSH] === FIM DA SAÍDA DESCARTADA ===")
            return "" # Retorna vazio, o que causa o erro na 'main'
        
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

# --- 3. LÓGICA DE PARSING (NOVO PARSER ÚNICO) ---

def _normalize_interface_name(name: str) -> str:
    """
    Normaliza o nome da interface. Para o Juniper,
    os nomes já são curtos (ex: et-0/0/9), então apenas limpamos.
    """
    return name.strip().replace(",", "") # Remove vírgulas (ex: "et-0/0/9,")

def _parse_speed_to_bps(speed_str: str | None) -> int | None:
    """Converte '100Gbps' ou '10g' em 100000000000."""
    if not speed_str:
        return None
    
    speed_str = speed_str.lower()
    multipliers = {'k': 10**3, 'm': 10**6, 'g': 10**9, 't': 10**12}
    
    match = re.search(r'([\d\.]+)\s*([kmgt]?)bps', speed_str)
    if not match:
        match = re.search(r'([\d\.]+)\s*([kmgt]?)', speed_str)
        if not match:
            return None

    try:
        value = float(match.group(1))
        multiplier_char = match.group(2)
        multiplier = multipliers.get(multiplier_char, 1)
        return int(value * multiplier)
    except Exception:
        return None

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
        
        target_match = re.search(r"(-?[\d\.]+)\s*" + target_unit_regex, content)
        if target_match:
            return float(target_match.group(1))
        
        first_float_match = re.search(r"(-?[\d\.]+)", content)
        if first_float_match:
            return float(first_float_match.group(1))
            
    except (ValueError, TypeError, AttributeError):
        pass
    return None

def _get_juniper_string(key_regex: str, text: str) -> str | None:
    """Helper para extrair um string de uma linha do Juniper."""
    try:
        line_match = re.search(r"^\s*" + key_regex + r"\s*:\s*(.*)$", text, re.MULTILINE)
        if line_match:
            value = line_match.group(1).strip()
            return value if value and value != "N/A" else None
    except Exception:
        pass
    return None
    
def _get_juniper_int(key_regex: str, text: str) -> int | None:
    """Helper para extrair um inteiro de uma linha do Juniper."""
    try:
        line_match = re.search(r"^\s*" + key_regex + r"\s*:\s*([\d]+)", text, re.MULTILINE)
        if line_match:
            return int(line_match.group(1))
    except (ValueError, TypeError, AttributeError):
        pass
    return 0 # Default para contadores de erro é 0

def _parse_single_interface_block(output_text: str) -> dict | None:
    """
    Analisa um *único bloco* de 'show interfaces extensive' do JUNIPER.
    Retorna um dicionário com todos os dados (status, stats, module, reading).
    """
    
    status_data = {}
    stats_data = {}
    module_data = {}
    reading_data = {}

    # --- 1. Status (Up/Down) ---
    # Ex: Physical interface: et-0/0/9, Enabled, Physical link is Up
    status_match = re.search(r"Enabled|Administratively down", output_text)
    if status_match:
        status_str = status_match.group(0)
        status_data['protocol_status'] = 'up' if status_str == 'Enabled' else 'down'
    
    link_match = re.search(r"Physical link is (Up|Down)", output_text)
    if link_match:
        status_data['physical_status'] = link_match.group(1).lower()
    
    # --- 2. Estatísticas (Erros e Utilização) ---
    # Extrai blocos de Erro
    input_errors_text = ""
    in_err_match = re.search(r"Input errors:([\s\S]*?)(?=^\s*Output errors:)", output_text, re.MULTILINE)
    if in_err_match:
        input_errors_text = in_err_match.group(1)
        
    output_errors_text = ""
    out_err_match = re.search(r"Output errors:([\s\S]*?)(?=^\s*Statistics last cleared:)", output_text, re.MULTILINE)
    if out_err_match:
        output_errors_text = out_err_match.group(1)

    # Coleta de Erros
    stats_data['in_errors'] = _get_juniper_int(r"Errors", input_errors_text)
    stats_data['out_errors'] = _get_juniper_int(r"Errors", output_errors_text)
    stats_data['in_crc_errors'] = _get_juniper_int(r"CRC/Align errors", input_errors_text) # Erro específico

    # Cálculo de Utilização
    try:
        speed_str = _get_juniper_string(r"Speed", output_text)
        port_speed_bps = _parse_speed_to_bps(speed_str)
        
        in_bps_float = _get_juniper_float(r"Input  bytes", output_text, r"bps")
        out_bps_float = _get_juniper_float(r"Output bytes", output_text, r"bps")
        in_bps = int(in_bps_float) if in_bps_float is not None else None
        out_bps = int(out_bps_float) if out_bps_float is not None else None
       

        if port_speed_bps and port_speed_bps > 0 and in_bps is not None and out_bps is not None:
            stats_data['in_uti'] = round((in_bps / port_speed_bps) * 100, 2)
            stats_data['out_uti'] = round((out_bps / port_speed_bps) * 100, 2)
        else:
            # print(f"       [PARSE-STATS] Não foi possível calcular Uti%. Speed='{speed_str}', In='{in_bps}', Out='{out_bps}'")
            stats_data['in_uti'] = 0.0
            stats_data['out_uti'] = 0.0

    except Exception as e:
        print(f"       [ERRO-PARSE-STATS] Falha ao calcular Uti%: {e}")
        stats_data['in_uti'] = 0.0
        stats_data['out_uti'] = 0.0

    
    # --- 3. Ópticos (Módulo e Leituras) ---
    module_info_match = re.search(r"^\s*Module:([\s\S]*?)(?=^\s*\w)", output_text, re.MULTILINE)
    diag_info_match = re.search(r"^\s*Transceiver diagnostic:([\s\S]*)", output_text, re.MULTILINE)
    
    if not diag_info_match:
        if "transceiver is not supported" in output_text: return None
        if not module_info_match:
             reading_data['transceiver_status'] = "absent"
             module_data['serial_number'] = None
        else:
             reading_data['transceiver_status'] = "no_diag"
             module_text = module_info_match.group(1)
             module_data['serial_number'] = _get_juniper_string(r"Vendor S/N", module_text)
             module_data['vendor_part_number'] = _get_juniper_string(r"Vendor P/N", module_text)
             module_data['vendor_name'] = _get_juniper_string(r"Vendor Name", module_text)
        
        return {"status": status_data, "stats": stats_data, "module": module_data, "reading": reading_data}

    # SFP está presente e tem diagnóstico
    reading_data['transceiver_status'] = "present"
    diag_text = diag_info_match.group(1)
    
    # Leituras Dinâmicas
    reading_data['temperature'] = _get_juniper_float(r"Module temperature", diag_text, r"degrees C")
    reading_data['voltage'] = _get_juniper_float(r"Module voltage", diag_text, r"V")
    
    lane_text_block = diag_text
    lane_0_match = re.search(r"^\s*Lane 0([\s\S]*?)(?=^\s*Lane 1|^\s*$)", diag_text, re.MULTILINE)
    if lane_0_match:
        lane_text_block = lane_0_match.group(1)
    
    reading_data['bias_current'] = _get_juniper_float(r"Laser bias current", lane_text_block, r"mA")
    reading_data['tx_power'] = _get_juniper_float(r"Laser output power", lane_text_block, r"dBm")
    reading_data['rx_power'] = _get_juniper_float(r"Laser receiver power", lane_text_block, r"dBm")

    # Thresholds (Estáticos)
    module_data['temp_high'] = _get_juniper_float(r"Module temperature high alarm threshold", diag_text, r"degrees C")
    module_data['temp_low'] = _get_juniper_float(r"Module temperature low alarm threshold", diag_text, r"degrees C")
    module_data['temp_high_warning'] = _get_juniper_float(r"Module temperature high warning threshold", diag_text, r"degrees C")
    module_data['temp_low_warning'] = _get_juniper_float(r"Module temperature low warning threshold", diag_text, r"degrees C")
    module_data['volt_high'] = _get_juniper_float(r"Module voltage high alarm threshold", diag_text, r"V")
    module_data['volt_low'] = _get_juniper_float(r"Module voltage low alarm threshold", diag_text, r"V")
    module_data['volt_high_warning'] = _get_juniper_float(r"Module voltage high warning threshold", diag_text, r"V")
    module_data['volt_low_warning'] = _get_juniper_float(r"Module voltage low warning threshold", diag_text, r"V")
    module_data['bias_high'] = _get_juniper_float(r"Laser bias current high alarm threshold", diag_text, r"mA")
    module_data['bias_low'] = _get_juniper_float(r"Laser bias current low alarm threshold", diag_text, r"mA")
    module_data['bias_high_warning'] = _get_juniper_float(r"Laser bias current high warning threshold", diag_text, r"mA")
    module_data['bias_low_warning'] = _get_juniper_float(r"Laser bias current low warning threshold", diag_text, r"mA")
    module_data['tx_power_high'] = _get_juniper_float(r"Laser output power high alarm threshold", diag_text, r"dBm")
    module_data['tx_power_low'] = _get_juniper_float(r"Laser output power low alarm threshold", diag_text, r"dBm")
    module_data['tx_power_high_warning'] = _get_juniper_float(r"Laser output power high warning threshold", diag_text, r"dBm")
    module_data['tx_power_low_warning'] = _get_juniper_float(r"Laser output power low warning threshold", diag_text, r"dBm")
    module_data['rx_power_high'] = _get_juniper_float(r"Laser rx power high alarm threshold", diag_text, r"dBm")
    module_data['rx_power_low'] = _get_juniper_float(r"Laser rx power low alarm threshold", diag_text, r"dBm")
    module_data['rx_power_high_warning'] = _get_juniper_float(r"Laser rx power high warning threshold", diag_text, r"dBm")
    module_data['rx_power_low_warning'] = _get_juniper_float(r"Laser rx power low warning threshold", diag_text, r"dBm")
    
    # Dados Estáticos (Módulo)
    if module_info_match:
        module_text = module_info_match.group(1)
        module_data['serial_number'] = _get_juniper_string(r"Vendor S/N", module_text)
        module_data['vendor_part_number'] = _get_juniper_string(r"Vendor P/N", module_text)
        module_data['vendor_name'] = _get_juniper_string(r"Vendor Name", module_text)
        module_data['connector_type'] = _get_juniper_string(r"Connector", module_text)
        module_data['wavelength_nm'] = _get_juniper_string(r"Wavelength", module_text)
        if not module_data.get('vendor_part_number'):
            module_data['transceiver_type'] = "Type Unknown"
        else:
             module_data['transceiver_type'] = module_data['vendor_part_number']
    
    # Limpa valores nulos (None) dos dicionários
    clean_status_data = {k: v for k, v in status_data.items() if v is not None}
    clean_stats_data = {k: v for k, v in stats_data.items() if v is not None}
    clean_module_data = {k: v for k, v in module_data.items() if v is not None}
    clean_reading_data = {k: v for k, v in reading_data.items() if v is not None}

    return {
        "status": clean_status_data, 
        "stats": clean_stats_data, 
        "module": clean_module_data, 
        "reading": clean_reading_data
    }

def parse_global_extensive_output(global_output_text: str) -> dict:
    """
    Analisa a saída completa de 'show interfaces extensive' e 
    retorna um dicionário de dados (status, stats, module, reading) por interface.
    """
    all_data = {}
    
    # Regex para encontrar o início de cada bloco de interface
    interface_header_regex = r"^Physical interface:\s*([A-Za-z0-9-./]+)"
    
    headers = list(re.finditer(interface_header_regex, global_output_text, re.MULTILINE))
    
    if not headers:
        print("     [PARSE-GLOBAL] Nenhuma 'Physical interface' encontrada na saída.")
        return {}

    print(f"     [PARSE-GLOBAL] Encontrados {len(headers)} blocos de 'Physical interface' na saída.")
    
    for i, header_match in enumerate(headers):
        try:
            long_name = header_match.group(1)
            interface_name = _normalize_interface_name(long_name)
            
            start_index = header_match.start()
            end_index = headers[i + 1].start() if (i + 1) < len(headers) else len(global_output_text)
            interface_block_text = global_output_text[start_index:end_index]
            
            # Silenciado para não poluir o log de teste
            # print(f"\n     --- [IFACE-PARSE] Processando Bloco para: {long_name} (como {interface_name}) ---")
            
            parsed_data = _parse_single_interface_block(interface_block_text) 
            
            if parsed_data:
                all_data[interface_name] = parsed_data
            # else:
                # Silenciado para não poluir
                # print(f"       [PARSE-BLOCK] Interface {long_name} ({interface_name}) ignorada (não-óptica ou sem dados).")
        except Exception as e:
            print(f"     [ERRO-PARSE] Erro fatal ao processar bloco para {long_name}: {e}")

    print(f"\n     [PARSE-GLOBAL] Análise concluída. {len(all_data)} interfaces com dados extraídos.")
    return all_data


# --- 4. ORQUESTRAÇÃO (MAIN) ---

async def process_device_monitoring(db: Prisma, dev: Device, semaphore: asyncio.Semaphore):
    """
    Processa um UNICO dispositivo: coleta, parseia e decide se salva ou printa.
    """
    
    # Comando único que pega TUDO
    COMMAND = "show interfaces extensive"
    
    async with semaphore:
        print(f"\n--- [DEV] Iniciando Processamento: {dev.hostname} (IP: {dev.ip_address}) ---")

        try:
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
            print(f"   [INFO] Encontradas {len(db_interfaces)} interfaces no DB. Buscando dados completos...")
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
        
        # --- !!! ESTA É A CORREÇÃO !!! ---
        # A verificação ' "error:" in raw_output.lower()' foi removida.
        # A função 'get_ssh_output' já trata erros de SSH (stderr)
        # e a saída do comando PODE conter "Input errors:", o que é normal.
        if not raw_output:
            print(f"   [ERRO-SSH] Falha ao obter dados de {dev.ip_address} (saída vazia ou falha na conexão). Pulando dispositivo.")
            return
        # --- FIM DA CORREÇÃO ---

        try:
            all_parsed_data = parse_global_extensive_output(raw_output)
        except Exception as e:
            print(f"   [ERRO-PARSE] Falha ao analisar dados de {dev.hostname}: {e}")
            return

        if not all_parsed_data:
            print(f"   [WARN] Parser não encontrou dados na saída de {dev.ip_address}.")
            return
            
        
        # --- PARTE 4: Salvar no DB ou Printar no Console ---
        
        stats_salvas = 0
        status_atualizado = 0
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
            status_data = parsed_data.get('status', {})
            stats_data = parsed_data.get('stats', {})
            module_data = parsed_data.get('module', {})
            reading_data = parsed_data.get('reading', {})
            
            if SAVE_TO_DATABASE:
                # --- LÓGICA DE BANCO DE DADOS ATIVADA ---
                
                # 4a. Salva InterfaceStats (Uti% e Erros)
                try:
                    # --- CORREÇÃO ---
                    # Criamos um dicionário 'data_to_save' apenas com os campos
                    # que REALMENTE existem no 'schema.prisma', pois 'stats_data'
                    # contém 'in_crc_errors' (que não está no schema).
                    data_to_save = {
                        'interface_id': iface.id,
                        'in_uti': stats_data.get('in_uti'),
                        'out_uti': stats_data.get('out_uti'),
                        'in_errors': stats_data.get('in_errors'),
                        'out_errors': stats_data.get('out_errors')
                    }
                    
                    await db.interfacestats.create(data=data_to_save)
                    stats_salvas += 1
                except Exception as e:
                    print(f"     [ERRO-DB-STATS] Falha ao salvar stats de {iface.interface_name}: {e}")

                # 4b. Atualiza NetworkInterface (Status Up/Down)
                try:
                    if status_data: # Só atualiza se tiver dados
                        await db.networkinterface.update(
                            where={'id': iface.id},
                            data=status_data
                        )
                        status_atualizado += 1
                except Exception as e:
                    print(f"     [ERRO-DB-STATUS] Falha ao atualizar status de {iface.interface_name}: {e}")

                # 4c. Salva TransceiverReading (Temp, Rx, Tx)
                try:
                    if reading_data.get('transceiver_status'): # Só salva se tiver status
                        reading_data['interface_id'] = iface.id
                        await db.transceiverreading.create(data=reading_data)
                        readings_salvas += 1
                except Exception as e:
                    print(f"     [ERRO-DB-READ] Falha ao salvar leitura de {iface.interface_name}: {e}")
                
                # 4d. Salva TransceiverModule (S/N e Thresholds) - CONDICIONAL
                try:
                    last_module = iface.modules[0] if iface.modules else None
                    last_serial = last_module.serial_number if last_module and last_module.serial_number else None
                    new_serial = module_data.get('serial_number')

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
                # Adiciona os dados parseados (para as interfaces que monitoramos)
                # ao dicionário que será impresso no final.
                print_output_data[iface_name] = parsed_data

        # --- ESTE É O LOCAL CORRETO PARA O PRINT/RESUMO ---
        if SAVE_TO_DATABASE:
            print(f"\n--- [DEV] Concluído Processamento de {dev.hostname} (Modo: Salvar) ---")
            print(f"   - {status_atualizado} interfaces com status (up/down) atualizado.")
            print(f"   - {stats_salvas} novos registros de estatísticas (Uti%/Erros) salvos.")
            print(f"   - {readings_salvas} novas leituras de transceiver (Temp/Rx/Tx) salvas.")
            print(f"   - {modules_salvos} novos eventos de módulo (S/N) registrados.")
        else:
            # Imprime o dicionário completo UMA VEZ por dispositivo
            print(f"\n--- [DEV] Concluído Processamento de {dev.hostname} (Modo: Console) ---")
            pprint.pprint(print_output_data)


async def main():
    db = Prisma()
    
    MAX_CONCURRENT_TASKS = 20 # Reduzido para o comando 'extensive'
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
    
    print("[INFO] Iniciando script de MONITORAMENTO COMPLETO (Juniper)...")
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
        
        # Esta consulta está funcionando, com base nos seus logs
        devices = await db.device.find_many(
             where={'os': 'junos'}
        )
        
        if not devices:
            print("[ERRO] Nenhum dispositivo com 'os' == 'junos' encontrado no banco. Execute o script de inventário primeiro.")
            return

        print(f"[INFO] Encontrados {len(devices)} dispositivos. Criando tarefas...")
        
        tasks = []
        for dev in devices:
            tasks.append(process_device_monitoring(db, dev, semaphore))
        
        await asyncio.gather(*tasks)
        
        end_total_time = time.time()
        
        print("\n==============================================")
        print(f"[INFO] Monitoramento COMPLETO concluído para todos os {len(devices)} dispositivos.")
        print(f"[INFO] Tempo total de execução: {end_total_time - start_total_time:.2f} segundos.")
        
    except Exception as e:
        print(f"\n[ERRO FATAL] Ocorreu um erro: {e}")
    finally:
        if db.is_connected():
            await db.disconnect()
            print("[INFO] Desconectado do banco de dados.")

if __name__ == "__main__":
    asyncio.run(main())