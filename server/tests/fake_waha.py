"""An in-memory stand-in for WAHA's key API, shared by the key tests (0027).

Flags make it misbehave in the ways the KeyManager guards against."""

from botsapp.waha_keys import WAHA_ACTIONS, KeyApiError, KeyInfo


class FakeWaha:
    """In-memory WAHA keys. Flags make it misbehave in the ways we guard against."""

    def __init__(self) -> None:
        self.keys: dict[str, KeyInfo] = {}
        self.n = 0
        self.apply_actions: dict[str, bool] | None = None  # ignore the request, use these
        self.make_admin = False
        self.wrong_session = False
        self.no_secret = False
        self.fail: set[str] = set()
        self.deleted: list[str] = []

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail:
            raise KeyApiError(f"{op} failed")

    async def create_key(self, *, session: str, actions: dict[str, bool]) -> KeyInfo:
        self._maybe_fail("create")
        self.n += 1
        info = KeyInfo(
            id=f"key_{self.n}",
            session="other" if self.wrong_session else session,
            is_admin=self.make_admin,
            is_active=True,
            actions={**{a: False for a in WAHA_ACTIONS}, **(self.apply_actions or actions)},
            secret=None if self.no_secret else f"wk_secret_{self.n}",
        )
        self.keys[info.id] = info
        return info

    async def get_key(self, key_id: str) -> KeyInfo | None:
        self._maybe_fail("get")
        info = self.keys.get(key_id)
        return (
            None
            if info is None
            else KeyInfo(info.id, info.session, info.is_admin, info.is_active, info.actions)
        )

    async def update_key_actions(self, key_id: str, actions: dict[str, bool]) -> KeyInfo:
        self._maybe_fail("update")
        current = self.keys[key_id]
        self.keys[key_id] = KeyInfo(
            key_id,
            current.session,
            current.is_admin,
            current.is_active,
            {**{a: False for a in WAHA_ACTIONS}, **(self.apply_actions or actions)},
        )
        return self.keys[key_id]

    async def delete_key(self, key_id: str) -> None:
        self._maybe_fail("delete")
        self.deleted.append(key_id)
        self.keys.pop(key_id, None)
