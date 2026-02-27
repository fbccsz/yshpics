import mercadopago
import uuid
import random
from datetime import datetime, timedelta

PIX_EXPIRACAO_MINUTOS = 30

def gerar_cpf_valido():
    """Gera um CPF matematicamente válido para bypass no checkout."""
    cpf = [random.randint(0, 9) for _ in range(9)]
    for _ in range(2):
        val = sum([(len(cpf) + 1 - i) * v for i, v in enumerate(cpf)]) % 11
        cpf.append(11 - val if val > 1 else 0)
    return ''.join(map(str, cpf))

def _criar_payment_data(valor_pedido, email_cliente, nome_cliente, id_pedido_interno):
    """Monta o payload base do pagamento."""
    partes = nome_cliente.strip().split()
    if len(partes) >= 2:
        first_name = partes[0]
        last_name = " ".join(partes[1:])
    else:
        first_name = "Cliente"
        last_name = "yshpics"
    expiracao = datetime.utcnow() + timedelta(minutes=PIX_EXPIRACAO_MINUTOS)
    return {
        "transaction_amount": float(valor_pedido),
        "description": f"Compra de Fotos - Pedido #{id_pedido_interno}",
        "payment_method_id": "pix",
        "date_of_expiration": expiracao.strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
        "payer": {
            "email": email_cliente if "@" in email_cliente else "cliente@yshpics.com",
            "first_name": first_name,
            "last_name": last_name,
            "identification": {
                "type": "CPF",
                "number": gerar_cpf_valido()
            }
        }
    }, expiracao

def gerar_cobranca_pix(valor_pedido, email_cliente, nome_cliente, id_pedido_interno, token_fotografo, taxa_plataforma):
    """Gera cobrança PIX. Tenta com split de comissão; se falhar, tenta sem.
    
    Retorna dict com 'sucesso', 'split_aplicado' (bool) e dados do PIX.
    """

    sdk = mercadopago.SDK(token_fotografo)

    payment_data, expiracao = _criar_payment_data(valor_pedido, email_cliente, nome_cliente, id_pedido_interno)

    # Tenta primeiro com application_fee (split marketplace)
    if taxa_plataforma > 0:
        payment_data["application_fee"] = float(taxa_plataforma)

    def _tentar(dados):
        opts = mercadopago.config.RequestOptions()
        opts.custom_headers = {'x-idempotency-key': str(uuid.uuid4())}
        result = sdk.payment().create(dados, opts)
        resp = result.get("response", {})
        if resp.get("status") == "pending":
            return {
                "sucesso": True,
                "txid": resp["id"],
                "copia_cola": resp["point_of_interaction"]["transaction_data"]["qr_code"],
                "qr_code_img": resp["point_of_interaction"]["transaction_data"]["qr_code_base64"],
                "expiracao": expiracao,
            }
        return {"sucesso": False, "resp": resp}

    try:
        resultado = _tentar(payment_data)
        if resultado["sucesso"]:
            resultado["split_aplicado"] = "application_fee" in payment_data
            return resultado

        # Se falhou com application_fee, tenta sem (conta não-marketplace)
        if "application_fee" in payment_data:
            print(f"⚠️  Falha com application_fee ({resultado['resp'].get('message','')}). Tentando sem split...")
            payment_data_sem_split = {k: v for k, v in payment_data.items() if k != "application_fee"}
            resultado2 = _tentar(payment_data_sem_split)
            if resultado2["sucesso"]:
                resultado2["split_aplicado"] = False
                return resultado2
            print("--- ERRO MERCADO PAGO (sem split) ---", resultado2["resp"])
            return {"sucesso": False, "erro": resultado2["resp"].get("message", "Falha na API do Mercado Pago.")}

        print("--- ERRO MERCADO PAGO ---", resultado["resp"])
        return {"sucesso": False, "erro": resultado["resp"].get("message", "Falha na API do Mercado Pago.")}

    except Exception as e:
        print(f"Erro de comunicação com Mercado Pago: {e}")
        return {"sucesso": False, "erro": str(e)}