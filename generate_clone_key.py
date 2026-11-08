"""Print a Fernet key for CLONE_ENCRYPTION_KEY; generate it once and keep it safe."""

from cryptography.fernet import Fernet


if __name__ == "__main__":
    print(Fernet.generate_key().decode("ascii"))
