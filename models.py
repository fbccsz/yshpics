import os
import uuid
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey, Text, text
from sqlalchemy.orm import declarative_base, relationship
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_engine(DATABASE_URL)
Base = declarative_base()

# ==========================================
# 1. A TABELA DO DONO DO NEGÓCIO (SaaS)
# ==========================================
class Fotografo(Base):
    __tablename__ = "fotografos"
    
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    senha_hash = Column(String, nullable=False) # Para o painel administrativo dele
    
    # Regra de Negócio e Split
    plano_atual = Column(String, default="starter") # 'starter' (10%) ou 'pro' (0%)
    
    # Credenciais do Mercado Pago dele (Onde a grana vai cair)
    mp_user_id = Column(String, nullable=True) 
    mp_access_token = Column(String, nullable=True)

    # Ligações
    albuns = relationship("Album", back_populates="fotografo")
    pedidos = relationship("Pedido", back_populates="fotografo")


# ==========================================
# 2. O COMPRADOR (Guest Checkout)
# ==========================================
class Cliente(Base):
    __tablename__ = "clientes"
    
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String)
    email = Column(String)
    cpf = Column(String, nullable=True) # Essencial para o PIX/Cartão
    
    pedidos = relationship("Pedido", back_populates="cliente")


# ==========================================
# 3. AS GALERIAS E FOTOS
# ==========================================
class Album(Base):
    __tablename__ = "albuns"
    
    id = Column(Integer, primary_key=True, index=True)
    titulo = Column(String)
    hash_url = Column(String, unique=True)
    data_evento = Column(DateTime, default=datetime.utcnow)
    categoria = Column(String, nullable=True)   # Ex: Esportes, Festas, Formaturas
    cidade = Column(String, nullable=True)       # Ex: Salvador/BA

    # Agora o álbum tem um dono!
    fotografo_id = Column(Integer, ForeignKey("fotografos.id"))
    fotografo = relationship("Fotografo", back_populates="albuns")
    
    fotos = relationship("Foto", back_populates="album")

class Foto(Base):
    __tablename__ = "fotos"
    
    id = Column(Integer, primary_key=True, index=True)
    album_id = Column(Integer, ForeignKey("albuns.id"))
    
    # O Cofre e a Vitrine
    caminho_baixa_res = Column(String) # Pública (Marca d'água)
    caminho_alta_res = Column(String)  # Privada (Original)
    
    preco_baixa = Column(Float)
    preco_alta = Column(Float)
    
    album = relationship("Album", back_populates="fotos")


# ==========================================
# 4. O FINANCEIRO E A LIBERAÇÃO
# ==========================================
class Pedido(Base):
    __tablename__ = "pedidos"
    
    id = Column(Integer, primary_key=True, index=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"))
    
    # Para sabermos para quem enviar o dinheiro no Split
    fotografo_id = Column(Integer, ForeignKey("fotografos.id"))
    
    valor_total = Column(Float, default=0.0)
    taxa_plataforma = Column(Float, default=0.0) # Registra o seu lucro (R$ 2,00 no Starter, R$ 0 no Pro)
    
    status_pagamento = Column(String, default="Pendente") # 'Pendente', 'Pago', 'Cancelado'
    data_pedido = Column(DateTime, default=datetime.utcnow)
    
    # Dados do Mercado Pago
    pix_txid = Column(String, nullable=True)
    pix_copia_cola = Column(String, nullable=True)
    pix_qr_code_base64 = Column(Text, nullable=True) # TIPO TEXT PARA NÃO QUEBRAR MAIS!
    
    # Guest Checkout: O token mágico de download sem senha
    token_download = Column(String, default=lambda: str(uuid.uuid4()))

    # PIX: data/hora em que o código expira (30 min após criação)
    pix_expiracao = Column(DateTime, nullable=True)

    cliente = relationship("Cliente", back_populates="pedidos")
    fotografo = relationship("Fotografo", back_populates="pedidos")
    itens = relationship("ItemPedido", back_populates="pedido")


# ==========================================
# 5. CONFIGURAÇÃO DA PLATAFORMA
# ==========================================
class PlataformaConfig(Base):
    __tablename__ = "plataforma_config"

    id = Column(Integer, primary_key=True, index=True)
    # Quando o owner resetou as métricas pela última vez (None = sem reset)
    metricas_reset_em = Column(DateTime, nullable=True)


class ItemPedido(Base):
    __tablename__ = "itens_pedido"
    
    id = Column(Integer, primary_key=True, index=True)
    pedido_id = Column(Integer, ForeignKey("pedidos.id"))
    foto_id = Column(Integer, ForeignKey("fotos.id"))
    qualidade = Column(String) 
    preco_cobrado = Column(Float)
    
    pedido = relationship("Pedido", back_populates="itens")
    foto = relationship("Foto")

# Cria as tabelas no banco de dados
Base.metadata.create_all(bind=engine)

# Migração: adiciona colunas que podem não existir em bancos já criados
with engine.connect() as conn:
    conn.execute(
        text("ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS pix_expiracao TIMESTAMP")
    )
    conn.execute(
        text("ALTER TABLE albuns ADD COLUMN IF NOT EXISTS categoria VARCHAR")
    )
    conn.execute(
        text("ALTER TABLE albuns ADD COLUMN IF NOT EXISTS cidade VARCHAR")
    )
    conn.commit()