"""Create a secret, non-overwriting production environment; no paid services enabled."""
import os
import secrets
from pathlib import Path

env=Path('.env')
if env.exists():
    raise SystemExit('.env exists; back it up and review it manually. Nothing overwritten.')
password=secrets.token_urlsafe(24)
values={'POSTGRES_PASSWORD':password,'MJ_DATABASE_URL':f'postgresql+psycopg://mj:{password}@postgres:5432/mj',
        'MJ_MODE':'production','MJ_DATA_DIR':'/data','MJ_ADMIN_PASSWORD':secrets.token_urlsafe(24),
        'MJ_COMFYUI_ENABLED':'false','MJ_EXTERNAL_ENABLED':'false','MJ_PROVIDERS_FILE':'/app/providers.json',
        'MJ_SECURE_COOKIE':'false','MJ_PUBLIC_ORIGIN':'http://127.0.0.1:8080'}
fd=os.open(env,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:
    for key,value in values.items():f.write(f'{key}={value}\n')
if not Path('providers.json').exists():
    Path('providers.json').write_text('{}\n',encoding='utf-8')
print('Created .env and providers.json. Read the administrator password locally; do not commit either file.')
print('Run: docker compose up --build -d')
