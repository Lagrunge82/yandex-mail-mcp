"""Refresh tokens rotate: each works once, and a reused one revokes its authorization.

The fake Yandex hands back the same refresh token on every refresh, as a reusable
upstream token does, so the rotation and the reuse detection here are the server's
own and do not rely on Yandex.
"""

import asyncio
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from test_auth_server import PUBLIC, Clock, FakeYandex, _raw_keys, run_flow
from test_revocation import call_tool, imap  # noqa: F401 (fixture)

from yandex_mail_mcp.auth_server import (
    GENERATION_MARGIN,
    REFRESH_TTL,
    AuthorizationStore,
    ClientStore,
    YandexAuthProvider,
)
from yandex_mail_mcp.crypto import TokenCipher, generate_key
from yandex_mail_mcp.yandex_oauth import YandexOAuthError, YandexUser


@pytest.fixture(autouse=True)
def allowed_users(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_USERS", "ivan")
    monkeypatch.delenv("MCP_AUTH_RATE_LIMIT", raising=False)


async def refresh(flow, client_id, refresh_token):
    return await flow.client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )


def test_each_refresh_token_works_once_along_a_chain():
    async def scenario(flow):
        client_id, tokens = await flow.sign_in()
        chain = [tokens]
        for _ in range(3):
            response = await refresh(flow, client_id, chain[-1]["refresh_token"])
            assert response.status_code == 200, response.text
            chain.append(response.json())
        return chain

    chain, yandex = run_flow(scenario)
    assert len({tokens["refresh_token"] for tokens in chain}) == 4
    # The upstream token never changed: Yandex reuses it, the server rotates its own.
    assert yandex.refreshed == ["ya-refresh-yc"] * 3


@pytest.mark.parametrize("reused", [0, 1], ids=["the first token", "a middle token"])
def test_reused_refresh_token_revokes_the_whole_authorization(imap, reused):  # noqa: F811
    async def scenario(flow):
        client_id, tokens = await flow.sign_in()
        chain = [tokens]
        for _ in range(2):
            chain.append((await refresh(flow, client_id, chain[-1]["refresh_token"])).json())
        newest = chain[-1]
        replayed = await refresh(flow, client_id, chain[reused]["refresh_token"])
        return {
            "replayed": replayed,
            "newest_refresh": await refresh(flow, client_id, newest["refresh_token"]),
            "newest_access": await call_tool(flow, newest["access_token"], "list_folders"),
        }

    seen, _ = run_flow(scenario)
    assert seen["replayed"].status_code == 400
    assert seen["replayed"].json()["error"] == "invalid_grant"
    assert "already used" in seen["replayed"].json()["error_description"]
    # Whoever holds the newest pair, the user or someone with a copy, is out too.
    assert seen["newest_refresh"].json()["error"] == "invalid_grant"
    assert seen["newest_access"].status_code == 401


def test_reuse_in_one_authorization_leaves_another_alone(imap):  # noqa: F811
    async def scenario(flow):
        client_id, first = await flow.sign_in("yc")
        other_client, second = await flow.sign_in("yd")
        await refresh(flow, client_id, first["refresh_token"])
        await refresh(flow, client_id, first["refresh_token"])  # Reused: first is revoked.
        return (
            await refresh(flow, other_client, second["refresh_token"]),
            await call_tool(flow, second["access_token"], "list_folders"),
        )

    (other_refresh, other_call), _ = run_flow(
        scenario,
        yandex=FakeYandex(
            users={"yc": YandexUser("ivan", "ivan@yandex.ru")}
            | {"yd": YandexUser("ivan", "ivan@yandex.ru")}
        ),
    )
    assert other_refresh.status_code == 200
    assert not other_call.get("isError"), other_call


class Gated(FakeYandex):
    """A Yandex whose first refresh waits until the test lets it answer."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.go = threading.Event()

    def refresh(self, refresh_token, scope=""):
        if not self.entered.is_set():
            self.entered.set()
            assert self.go.wait(5)
        return super().refresh(refresh_token, scope)


def test_second_of_two_concurrent_refreshes_revokes_the_first_in_flight():
    yandex = Gated()

    async def scenario(flow):
        client_id, tokens = await flow.sign_in()
        first = asyncio.ensure_future(refresh(flow, client_id, tokens["refresh_token"]))
        # The first request has claimed the generation and waits on Yandex.
        await asyncio.to_thread(yandex.entered.wait, 5)
        second = await refresh(flow, client_id, tokens["refresh_token"])
        yandex.go.set()
        return await first, second

    (first, second), _ = run_flow(scenario, yandex=yandex)
    # Exactly one request claimed the generation; the other is the reuse, and it
    # revoked the sign-in before the first could hand out a pair.
    assert second.json()["error_description"].endswith("already used; sign in again")
    assert first.json() == {
        "error": "invalid_grant",
        "error_description": "the authorization was revoked",
    }


def test_claims_are_atomic_across_threads():
    generations = AuthorizationStore(":memory:")
    won = []
    barrier = threading.Barrier(16)

    def race():
        barrier.wait()
        won.append(generations.claim("a" * 32, 0))

    threads = [threading.Thread(target=race) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert won.count(True) == 1
    generations.close()


def test_failed_yandex_refresh_lets_the_same_token_be_used_again():
    class Flaky(FakeYandex):
        failures = 1

        def refresh(self, refresh_token, scope=""):
            if self.failures:
                self.failures -= 1
                raise YandexOAuthError("unavailable", "timed out")
            return super().refresh(refresh_token, scope)

    async def scenario(flow):
        client_id, tokens = await flow.sign_in()
        failed = await refresh(flow, client_id, tokens["refresh_token"])
        retried = await refresh(flow, client_id, tokens["refresh_token"])
        return failed, retried

    (failed, retried), _ = run_flow(scenario, yandex=Flaky())
    assert failed.json()["error"] == "invalid_grant"
    assert "Yandex refused" in failed.json()["error_description"]
    # Not a reuse: the failed exchange spent nothing.
    assert retried.status_code == 200, retried.text


@pytest.mark.parametrize("failure", ["error", "cancel"])
def test_exchange_stopped_by_anything_leaves_the_token_usable(failure):
    clock = Clock()
    cipher = TokenCipher(list(_raw_keys(generate_key())), clock)
    entered, go = threading.Event(), threading.Event()

    class Breaking(FakeYandex):
        broken = True

        def refresh(self, refresh_token, scope=""):
            if self.broken:
                self.broken = False
                if failure == "error":
                    raise RuntimeError("unexpected")
                entered.set()
                go.wait(5)
            return super().refresh(refresh_token, scope)

    server = provider(":memory:", cipher, clock, oauth=Breaking())
    issued = server._issue(
        "client", ["mail.read"], {**GRANT, "yandex_expires_at": clock.now + 3600}
    )

    async def stopped():
        loaded = await server.load_refresh_token(CLIENT, issued.refresh_token)
        task = asyncio.ensure_future(server.exchange_refresh_token(CLIENT, loaded, ["mail.read"]))
        if failure == "cancel":
            await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            go.set()
        with pytest.raises((RuntimeError, asyncio.CancelledError)):
            await task

    asyncio.run(stopped())
    # No new token went out, so the same one works and is not taken for a reuse.
    assert exchange(server, issued.refresh_token) is not None
    close(server)


def test_store_writes_run_off_the_event_loop(monkeypatch):
    clock = Clock()
    cipher = TokenCipher(list(_raw_keys(generate_key())), clock)
    server = provider(":memory:", cipher, clock)
    threads = []
    for name in ("claim", "revoke"):
        original = getattr(AuthorizationStore, name)

        def recording(self, *args, original=original, name=name):
            threads.append((name, threading.current_thread() is threading.main_thread()))
            return original(self, *args)

        monkeypatch.setattr(AuthorizationStore, name, recording)
    issued = server._issue(
        "client", ["mail.read"], {**GRANT, "yandex_expires_at": clock.now + 3600}
    )
    exchange(server, issued.refresh_token)
    with pytest.raises(Exception, match="already used"):
        exchange(server, issued.refresh_token)  # The reuse revokes: a write too.
    assert threads and all(not on_loop for _, on_loop in threads), threads
    assert {name for name, _ in threads} == {"claim", "revoke"}
    close(server)


def test_generation_outlives_the_refresh_token_issued_after_a_slow_yandex():
    clock = Clock()
    cipher = TokenCipher(list(_raw_keys(generate_key())), clock)

    class Slow(FakeYandex):
        def refresh(self, refresh_token, scope=""):
            clock.now += 5  # Yandex takes five seconds to answer.
            return super().refresh(refresh_token, scope)

    server = provider(":memory:", cipher, clock, oauth=Slow())
    start = clock.now
    first = server._issue("client", ["mail.read"], {**GRANT, "yandex_expires_at": clock.now + 3600})
    second = exchange(server, first.refresh_token)
    # The second refresh token is still valid three seconds before its end, which
    # falls after the end of a row kept for exactly REFRESH_TTL from the claim.
    clock.now = start + REFRESH_TTL + 2
    assert exchange(server, second.refresh_token) is not None
    close(server)


def test_expired_generations_are_removed_when_the_store_opens(tmp_path):
    clock = Clock()
    db = str(tmp_path / "auth.db")
    store = AuthorizationStore(db, clock)
    assert store.claim("a" * 32, 0)
    store.close()
    clock.now += REFRESH_TTL + GENERATION_MARGIN + 1
    AuthorizationStore(db, clock).close()
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("SELECT COUNT(*) FROM refresh_generations").fetchone() == (0,)
    finally:
        connection.close()


def provider(db, cipher, clock, oauth=None):
    return YandexAuthProvider(
        cipher=cipher,
        clients=ClientStore(db, cipher),
        oauth=oauth or FakeYandex(),
        public_url=PUBLIC,
        allow_write=False,
        allowed_users=frozenset({"ivan"}),
        clock=clock,
        authorizations=AuthorizationStore(db, clock),
    )


def close(*providers):
    for each in providers:
        each.clients.close()
        each.authorizations.close()


GRANT = {
    "authorization": "a" * 32,
    "yandex_access_token": "ya-access-yc",
    "yandex_refresh_token": "ya-refresh-yc",
    "yandex_scope": "mail:imap_ro",
    "login": "ivan",
    "email": "ivan@yandex.ru",
}
CLIENT = SimpleNamespace(client_id="client")


def exchange(server, refresh_token):
    async def run():
        loaded = await server.load_refresh_token(CLIENT, refresh_token)
        if loaded is None:
            return None
        return await server.exchange_refresh_token(CLIENT, loaded, ["mail.read"])

    return asyncio.run(run())


def test_rotation_survives_a_restart(tmp_path):
    clock = Clock()
    cipher = TokenCipher(list(_raw_keys(generate_key())), clock)
    db = str(tmp_path / "auth.db")
    before = provider(db, cipher, clock)
    first = before._issue("client", ["mail.read"], {**GRANT, "yandex_expires_at": clock.now + 3600})
    second = exchange(before, first.refresh_token)
    close(before)
    after = provider(db, cipher, clock)
    # After the restart the spent token is still spent, and using it revokes all.
    with pytest.raises(Exception, match="already used"):
        exchange(after, first.refresh_token)
    assert exchange(after, second.refresh_token) is None
    close(after)


def test_refresh_token_from_before_generations_is_generation_zero(tmp_path):
    clock = Clock()
    cipher = TokenCipher(list(_raw_keys(generate_key())), clock)
    server = provider(str(tmp_path / "auth.db"), cipher, clock)
    claims = {
        "client_id": "client",
        "scopes": ["mail.read"],
        "grant": {**GRANT, "yandex_expires_at": clock.now + 3600},
    }
    legacy = cipher.seal("refresh", claims, 3600)  # No generation in it.
    assert exchange(server, legacy) is not None
    with pytest.raises(Exception, match="already used"):
        exchange(server, legacy)
    close(server)


@pytest.mark.parametrize("generation", [-1, True, "1", 1.5])
def test_refresh_token_with_a_bad_generation_is_refused(generation):
    clock = Clock()
    cipher = TokenCipher(list(_raw_keys(generate_key())), clock)
    server = provider(":memory:", cipher, clock)
    claims = {
        "client_id": "client",
        "scopes": ["mail.read"],
        "grant": {**GRANT, "yandex_expires_at": clock.now + 3600},
        "generation": generation,
    }
    assert (
        asyncio.run(server.load_refresh_token(CLIENT, cipher.seal("refresh", claims, 3600))) is None
    )
    close(server)
