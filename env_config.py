"""Read local .env assignments without executing shell commands."""
import os
from pathlib import Path
import re
import shlex


def load_env(path=None):
    """Shell environment takes precedence; support optional `export` prefixes."""
    path = Path(path) if path is not None else Path(__file__).parent / '.env'
    if not path.exists():
        return
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        name, separator, value = line.partition('=')
        name = name.strip()
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
            raise ValueError(f'.env dòng {number}: cần TEN_BIEN=gia_tri.')
        try:
            parts = shlex.split(value, comments=True)
        except ValueError:
            raise ValueError(f'.env dòng {number}: dấu nháy không hợp lệ.') from None
        if len(parts) > 1:
            raise ValueError(f'.env dòng {number}: giá trị chứa khoảng trắng cần dấu nháy.')
        os.environ.setdefault(name, parts[0] if parts else '')
