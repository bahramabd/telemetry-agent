from __future__ import annotations

from urllib.parse import urlparse

from pymongo import MongoClient
from pymongo.database import Database

from src.config import Settings


def get_mongo_client(settings: Settings) -> MongoClient:
    return MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)


def get_database_name_from_uri(uri: str) -> str:
    parsed = urlparse(uri)
    path = parsed.path.lstrip("/")
    if path:
        return path.split("/")[0]
    return "telemetry"


def get_database(settings: Settings) -> Database:
    client = get_mongo_client(settings)
    database_name = get_database_name_from_uri(settings.mongo_uri)
    return client[database_name]


def ping_database(settings: Settings) -> bool:
    client = get_mongo_client(settings)
    try:
        client.admin.command("ping")
        return True
    except Exception:
        return False
    finally:
        client.close()
