from alembic import context
from sqlalchemy import create_engine
from mj.config import Settings
from mj.db import Base
from mj.cli import load_env
from pathlib import Path

load_env(Path('.env'))
settings=Settings.env()
if context.is_offline_mode():
    context.configure(url=settings.database_url,target_metadata=Base.metadata,literal_binds=True)
    with context.begin_transaction():context.run_migrations()
else:
    with create_engine(settings.database_url).connect() as connection:
        context.configure(connection=connection,target_metadata=Base.metadata)
        with context.begin_transaction():context.run_migrations()
