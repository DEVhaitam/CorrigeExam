"""
auth.py — JWT authentication helper for CorrectExam load tests.

Usage inside a Locust User:
    from workload.locust.auth import get_token, AuthMixin

    class MyUser(AuthMixin, HttpUser):
        def on_start(self):
            self.authenticate()
"""

import os
import logging

DEFAULT_USER = os.getenv("LOCUST_API_USER", "user")
DEFAULT_PASS = os.getenv("LOCUST_API_PASS", "user")


def get_token(client, username: str = DEFAULT_USER, password: str = DEFAULT_PASS) -> str:
    """POST /api/authenticate and return the id_token. Raises on failure."""
    resp = client.post(
        "/api/authenticate",
        json={"username": username, "password": password, "rememberMe": False},
        name="/api/authenticate",
    )
    if resp.status_code != 200:
        logging.error("Auth failed %s: %s", resp.status_code, resp.text[:300])
        resp.raise_for_status()
    return resp.json()["id_token"]


class AuthMixin:
    """
    Mixin that handles JWT acquisition and automatic re-auth on 401.

    Add to any HttpUser subclass. Call self.authenticate() in on_start.
    All tasks that need auth should call self.authed_get/post/patch.
    """

    _token: str = ""

    def authenticate(self) -> None:
        self._token = get_token(self.client)  # type: ignore[attr-defined]
        self.client.headers.update({"Authorization": f"Bearer {self._token}"})  # type: ignore[attr-defined]

    def _maybe_reauth(self, resp) -> bool:
        """Return True if a 401 was detected and re-auth succeeded."""
        if resp.status_code == 401:
            logging.warning("401 received — re-authenticating")
            try:
                self.authenticate()
                return True
            except Exception as exc:  # noqa: BLE001
                logging.error("Re-auth failed: %s", exc)
        return False
