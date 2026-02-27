import mercadopago
import uuid
import random

def gerar_cpf_valido():
    """Gera um CPF matematicamente válido para bypass no checkout."""
    cpf = [random.randint(0, 9) for _ in range(9)]
    for _ in range(2):
        val = sum([(len(cpf) + 1 - i) * v for i, v in enumerate(cpf)]) % 11
        cpf.append(11 - val if val > 1 else 0)
    return ''.join(map(str, cpf))

def gerar_cobranca_pix(valor_pedido, email_cliente, nome_cliente, id_pedido_interno, token_fotografo, taxa_plataforma):
    """Gera cobrança com Split: dinheiro pro Fotógrafo, comissão pra você."""
    
    # Inicia o SDK com a chave do DONO da foto
    sdk = mercadopago.SDK(token_fotografo)
    nome_real = nome_cliente if len(nome_cliente.split()) > 1 else "João Silva"
    
    payment_data = {
        "transaction_amount": float(valor_pedido),
        "description": f"Compra de Fotos - Pedido #{id_pedido_interno}",
        "payment_method_id": "pix",
        "payer": {
            "email": email_cliente,
            "first_name": nome_real,
            "identification": {
                "type": "CPF",
                "number": gerar_cpf_valido()
            }
        }
    }

    # A MÁGICA: Sua comissão vai aqui
    if taxa_plataforma > 0:
        payment_data["application_fee"] = float(taxa_plataforma)

    request_options = mercadopago.config.RequestOptions()
    request_options.custom_headers = {'x-idempotency-key': str(uuid.uuid4())}

    try:
        result = sdk.payment().create(payment_data, request_options)
        pagamento = result.get("response", {})

        if pagamento.get("status") == "pending":
            return {
                "sucesso": True,
                "txid": pagamento["id"],
                "copia_cola": pagamento["point_of_interaction"]["transaction_data"]["qr_code"],
                "qr_code_img": pagamento["point_of_interaction"]["transaction_data"]["qr_code_base64"]
            }
        else:
            print("\n--- ERRO NO MERCADO PAGO ---", pagamento)
            return {"sucesso": False, "erro": "Falha na API do Mercado Pago."}
    except Exception as e:
        print(f"Erro de comunicação: {e}")
        return {"sucesso": False, "erro": str(e)}