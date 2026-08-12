from cryptography.fernet import Fernet
from django.conf import settings


def _fernet():
    return Fernet(settings.ENCRYPTION_KEY)


def encrypt_password(plaintext):
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_password(ciphertext):
    if not ciphertext:
        return ""
    return _fernet().decrypt(ciphertext.encode()).decode()
