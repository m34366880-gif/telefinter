from __future__ import annotations

import os
import datetime as dt
from typing import Optional

from sqlalchemy import Column, Integer, String, Text, DateTime, create_engine, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker


DATA_DIR = os.getenv("APP_DATA_DIR", "/workspace/data")
DB_URL = os.getenv("APP_DATABASE_URL", f"sqlite:///{os.path.join(DATA_DIR, 'app.db')}")

os.makedirs(DATA_DIR, exist_ok=True)

engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {}, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


class Gift(Base):
    __tablename__ = "gifts"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    gif_url = Column(String(1024), nullable=True)
    telegram_file_id = Column(String(256), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=dt.datetime.utcnow)


class TelegramMedia(Base):
    __tablename__ = "telegram_media"
    id = Column(Integer, primary_key=True)
    file_id = Column(String(256), nullable=False, index=True)
    file_unique_id = Column(String(256), nullable=True)
    file_path = Column(String(512), nullable=True)
    mime_type = Column(String(128), nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    size = Column(Integer, nullable=True)
    caption = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=dt.datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("file_id", name="uq_telegram_media_file_id"),
    )


class TelegramState(Base):
    __tablename__ = "telegram_state"
    id = Column(Integer, primary_key=True, default=1)
    last_update_id = Column(Integer, nullable=True)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
