"""
CLI to reset an admin's dashboard password (e.g. when it is forgotten).

Usage (run in Render's Shell for the live site):
    python -m admin.reset_password <username> <new_password>
"""
import sys

from admin.auth import hash_password
from storage import store


def main():
    if len(sys.argv) != 3:
        print("Usage: python -m admin.reset_password <username> <new_password>")
        sys.exit(1)
    username, password = sys.argv[1], sys.argv[2]
    if len(password) < 8:
        print("Password must be at least 8 characters.")
        sys.exit(1)
    admin = store.get_admin_by_username(username)
    if not admin:
        print(f"No admin named '{username}'.")
        sys.exit(1)
    store.set_admin_password(admin["id"], hash_password(password))
    print(f"Password reset for '{username}'.")


if __name__ == "__main__":
    main()
