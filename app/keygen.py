"""Print a fresh ENCRYPTION_KEY: ``python -m app.keygen``."""

from app.security.crypto import generate_key

if __name__ == "__main__":
    print(generate_key())
