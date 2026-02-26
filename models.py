from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, create_engine
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime
import os
from dotenv import load_dotenv

Base = declarative_base()

class Cliente(Base):
    __tablename__ = 'clientes'
    id = Column(Integer, primary_key=True)
    nome = Column(String(100))
    email = Column(String(100))

class Album(Base):
    __tablename__ = 'albuns'
    id = Column(Integer, primary_key=True)
    titulo = Column(String(100))
    hash_url = Column(String(20), unique=True)
    data_evento = Column(DateTime, default=datetime.utcnow)
    fotos = relationship("Foto", back_populates="album")

class Foto(Base):
    __tablename__ = 'fotos'
    id = Column(Integer, primary_key=True)
    album_id = Column(Integer, ForeignKey('albuns.id'))
    caminho_baixa_res = Column(String(255))
    caminho_alta_res = Column(String(255))
    # AGORA TEMOS DOIS PREÇOS
    preco_baixa = Column(Float, default=5.0)
    preco_alta = Column(Float, default=15.0)
    album = relationship("Album", back_populates="fotos")

# NOVA TABELA: Registra exatamente o que o cliente escolheu na hora da compra
class ItemPedido(Base):
    __tablename__ = 'itens_pedido'
    id = Column(Integer, primary_key=True)
    pedido_id = Column(Integer, ForeignKey('pedidos.id'))
    foto_id = Column(Integer, ForeignKey('fotos.id'))
    qualidade = Column(String(10)) # Salvará 'baixa' ou 'alta'
    preco_cobrado = Column(Float)
    
    foto = relationship("Foto")

class Pedido(Base):
    __tablename__ = 'pedidos'
    id = Column(Integer, primary_key=True)
    cliente_id = Column(Integer, ForeignKey('clientes.id'))
    valor_total = Column(Float, default=0.0)
    status_pagamento = Column(String(20), default="Pendente")
    pix_txid = Column(String(100), unique=True)
    pix_copia_cola = Column(String(500))
    pix_qr_code_base64 = Column(String(2000))
    data_pedido = Column(DateTime, default=datetime.utcnow)

    cliente = relationship("Cliente")
    # O pedido agora se relaciona com os Itens, não diretamente com a Foto
    itens = relationship("ItemPedido")

# 1. Carrega as variáveis do arquivo .env
load_dotenv()

# 2. Pega a URL do banco (se não achar no .env, usa o SQLite como segurança)
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///banco_fotos.db")

# 3. Configura o Engine dinamicamente
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    # Se for SQLite, precisamos da regra de thread
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
    )
else:
    # Se for PostgreSQL (produção), a conexão é direta e nativa
    engine = create_engine(SQLALCHEMY_DATABASE_URL)

# 4. Cria as tabelas
Base.metadata.create_all(bind=engine)