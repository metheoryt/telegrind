from typing import Optional
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import BigInteger


class Model(AsyncAttrs, DeclarativeBase):
    pass


class Chat(Model):
    __tablename__ = 'chat'

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column('chat_id', BigInteger)
    sheet_url: Mapped[Optional[str]]


class File(Model):
    __tablename__ = 'file'

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[str]
    filename: Mapped[str]
