"""Test-wide settings.

Production password hashing uses Argon2 with deliberately expensive
parameters (~60 ms per hash or verify). Almost every API test seeds users and
logs in, so the suite swaps in the cheapest Argon2 parameters. This runs before
any test module imports the stores, which build their hashers at import time.
"""

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

_FAST_HASH = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=8, parallelism=1),))
PasswordHash.recommended = classmethod(lambda cls: _FAST_HASH)  # type: ignore[method-assign]
