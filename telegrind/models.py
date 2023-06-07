from typing import Optional
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Model(AsyncAttrs, DeclarativeBase):
    pass


class Chat(Model):
    __tablename__ = 'chat'

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int]
    sheet_url: Mapped[Optional[str]]