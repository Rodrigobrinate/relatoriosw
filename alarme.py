import asyncio
import requests
from prisma import Prisma
from typing import Optional, List
import sys
import traceback

# --- CONFIGURAÇÃO ---
NTFY_TOPIC_URL = "https://ntfy.example.com/alertas-rede-alta" 
UTILIZATION_THRESHOLD = 90.0
# --------------------

def send_ntfy_alert(device_name: str, interface_description: Optional[str], in_uti: float, out_uti: float):
    """
    Envia um alerta formatado para o servidor NTFY.
    (Esta função permanece a mesma)
    """
    
    alert_details = []
    if in_uti > UTILIZATION_THRESHOLD:
        alert_details.append(f"Entrada: {in_uti:.2f}%")
    if out_uti > UTILIZATION_THRESHOLD:
        alert_details.append(f"Saída: {out_uti:.2f}%")
    
    details_str = " | ".join(alert_details)
    iface_display = interface_description if interface_description else "(Sem descrição)"
    
    message = f"Dispositivo: {device_name}\nInterface: {iface_display}\nDetalhes: {details_str}"
    title = f"Uso de Banda Acima de {UTILIZATION_THRESHOLD}%"
    
    try:
        requests.post(
            NTFY_TOPIC_URL,
            data=message.encode('utf-8'),
            headers={"Title": title, "Priority": "high", "Tags": "warning,chart_up"}
        )
        print(f"✅ ALERTA NTFY ENVIADO: {message}")
        
    except Exception as e:
        print(f"❌ Erro ao enviar alerta NTFY: {e}")

async def check_latest_stats():
    """
    Busca o último registro de estatística de CADA interface no banco
    e verifica se algum ultrapassou o limite.
    """
    
    db = Prisma()
    await db.connect()
    
    print("Iniciando verificação de estatísticas...")
    
    try:
        
        # --- AQUI ESTÁ A CORREÇÃO DEFINITIVA ---
        # O TypeError sugere que 'order_by' como lista não é aceito com 'distinct'.
        # A lógica correta é: ordenar *todos* os stats por timestamp (do mais novo para o mais velho)
        # e então pegar o primeiro 'distinct' (único) de cada 'interface_id'.
        
        latest_stats = await db.interfacestats.find_many(
            distinct=['interface_id'],
            
            # Simplificamos o order_by para apenas o que importa: o timestamp
            order_by={'timestamp': 'desc'}, # <--- MUDANÇA PRINCIPAL
            
            include={
                'interface': {  # Inclui a interface pai
                    'include': {
                        'device': True # E o device pai da interface
                    }
                }
            }
        )
        
        print(f"Encontrados {len(latest_stats)} stats mais recentes para verificar...")
        alert_count = 0

        # 2. Iterar sobre os STATS (não mais sobre as interfaces)
        for latest_stat in latest_stats:
            
            # Pula se o stat não tiver uma interface ou device (órfão)
            if not latest_stat.interface or not latest_stat.interface.device:
                print(f"Stat ID {latest_stat.id} sem interface/device. Pulando.")
                continue

            # Pega os dados do device e da interface
            device_name = latest_stat.interface.device.hostname
            iface_desc = latest_stat.interface.description if latest_stat.interface.description else latest_stat.interface.interface_name
            
            # Trata valores nulos (None) como 0.0 para comparação
            in_uti_val = latest_stat.in_uti if latest_stat.in_uti is not None else 0.0
            out_uti_val = latest_stat.out_uti if latest_stat.out_uti is not None else 0.0
            
            # 3. A verificação do limite
            if in_uti_val > UTILIZATION_THRESHOLD or out_uti_val > UTILIZATION_THRESHOLD:
                print(f"  🔥 LIMITE ATINGIDO! {device_name} / {iface_desc} [In: {in_uti_val:.2f}%, Out: {out_uti_val:.2f}%]")
                
                send_ntfy_alert(
                    device_name=device_name,
                    interface_description=latest_stat.interface.description,
                    in_uti=in_uti_val,
                    out_uti=out_uti_val
                )
                alert_count += 1
            else:
                # print(f"  ✅ OK: {device_name} / {iface_desc} [In: {in_uti_val:.2f}%, Out: {out_uti_val:.2f}%]")
                pass
        
        print(f"\nVerificação concluída. {alert_count} alertas enviados.")

    except Exception as e:
        print(f"❌ Erro catastrófico durante a verificação:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        
    finally:
        if db.is_connected():
            await db.disconnect()
            print("Desconectado do banco de dados.")

if __name__ == "__main__":
    asyncio.run(check_latest_stats())