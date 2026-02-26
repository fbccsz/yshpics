import mercadopago
import os
import uuid
import random
from dotenv import load_dotenv

# Carrega variáveis do .env
load_dotenv()
ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
if not ACCESS_TOKEN:
    raise Exception("ACCESS_TOKEN do Mercado Pago não encontrado no .env. Adicione MP_ACCESS_TOKEN=seu_token no arquivo .env")
sdk = mercadopago.SDK(ACCESS_TOKEN)

def gerar_cpf_valido():
    """Gera um CPF matematicamente válido em milissegundos para o Mercado Pago aceitar o PIX."""
    cpf = [random.randint(0, 9) for _ in range(9)]
    for _ in range(2):
        val = sum([(len(cpf) + 1 - i) * v for i, v in enumerate(cpf)]) % 11
        cpf.append(11 - val if val > 1 else 0)
    return ''.join(map(str, cpf))

def gerar_cobranca_pix(valor_pedido, email_cliente, nome_cliente, id_pedido_interno):
    """
    Gera uma cobrança PIX via Mercado Pago.
    """
    nome_real = nome_cliente if len(nome_cliente.split()) > 1 else "João Silva"
    
    # 1. Monta os dados injetando o CPF válido automático
    payment_data = {
        "transaction_amount": float(valor_pedido),
        "description": f"Compra de Fotos - Pedido #{id_pedido_interno}",
        "payment_method_id": "pix",
        "payer": {
            "email": email_cliente,
            "first_name": nome_real,
            "identification": {
                "type": "CPF",
                "number": gerar_cpf_valido() # <-- A MÁGICA ACONTECE AQUI
            }
        }
    }

    # 2. Cria a chave de segurança obrigatória contra duplicidade
    request_options = mercadopago.config.RequestOptions()
    request_options.custom_headers = {
        'x-idempotency-key': str(uuid.uuid4())
    }

    print("\n[DEBUG] Dados enviados para o Mercado Pago:")
    print(payment_data)

    # 3. Envia a requisição passando as opções de segurança
    result = sdk.payment().create(payment_data, request_options)
    
    print("[DEBUG] Resposta bruta da API Mercado Pago:")
    print(result)
    pagamento = result.get("response", {})

    # Verificando se deu certo
    if pagamento.get("status") == "pending":
        id_transacao_mp = pagamento["id"]
        pix_copia_e_cola = pagamento["point_of_interaction"]["transaction_data"]["qr_code"]
        qr_code_base64 = pagamento["point_of_interaction"]["transaction_data"]["qr_code_base64"]
        
        print("\n--- PIX GERADO COM SUCESSO ---")
        print(f"ID da Transação (Salvar no Banco): {id_transacao_mp}")
        
        return {
            "sucesso": True,
            "txid": id_transacao_mp,
            "copia_cola": pix_copia_e_cola,
            "qr_code_img": qr_code_base64
        }
    else:
        print("\n--- ERRO AO GERAR PIX ---")
        print(pagamento)
        return {"sucesso": False, "erro": pagamento}

# --- Testando a Função ---
if __name__ == "__main__":
    dados_pix = gerar_cobranca_pix(
        valor_pedido=45.00,
        email_cliente="cliente.teste@email.com",
        nome_cliente="João Silva",
        id_pedido_interno=101 
    )