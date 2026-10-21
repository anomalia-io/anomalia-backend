"""Configuración compartida de pytest para el backend de Anomalia.

Fija ``DATABASE_URL`` apuntando al Postgres publicado en el host (el contenedor
``anomalia_postgres`` expone 5432) antes de que los tests importen ``app.*``.
"""
import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://anomal_user:anomal_password@127.0.0.1:5432/anomal_db",
)
