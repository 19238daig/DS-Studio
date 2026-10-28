"""Controlled PostgreSQL sources; no connections are created on import."""
from src.database.query import DatabaseService

__all__ = ['DatabaseService']
