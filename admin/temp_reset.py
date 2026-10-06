"""
TEMPORARY, ONE-TIME-USE: admin password reset endpoint for when the admin
account is locked out on a deployment with no direct database access (e.g.
Render free tier, no Shell). Protected by a random token that must match the
ADMIN_RESET_TOKEN env var - without that env var set, the route always 404s,
so it's inert unless explicitly enabled.

DELETE THIS FILE and its router include in main.py once the password has
been reset - do not leave this deployed. Also remove the ADMIN_RESET_TOKEN
env var from Render afterward.
"""
import os

from fastapi import APIRouter, Form
from fastapi.responses import PlainTextResponse

from admin.auth import hash_password
from storage import store

router = APIRouter(prefix="/admin/temp-reset")


@router.post("/password", response_class=PlainTextResponse)
def reset_password(token: str = Form(...), username: str = Form(...), new_password: str = Form(...)):
    expected = os.getenv("ADMIN_RESET_TOKEN")
    if not expected or token != expected:
        return PlainTextResponse("Not found", status_code=404)

    if len(new_password) < 8:
        return PlainTextResponse("Password must be at least 8 characters.", status_code=400)

    admin = store.get_admin_by_username(username)
    if not admin:
        store.create_admin(username, hash_password(new_password))
        return PlainTextResponse(f"Admin '{username}' created.")

    store.set_admin_password(admin["id"], hash_password(new_password))
    return PlainTextResponse(f"Password reset for '{username}'.")
