from cryptography.fernet import Fernet

from app.config import DB_ENCRYPTION_KEY

_fernet = Fernet(DB_ENCRYPTION_KEY.encode())


def encrypt_value(plain: str) -> str:
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_value(token: str) -> str:
    return _fernet.decrypt(token.encode()).decode()
