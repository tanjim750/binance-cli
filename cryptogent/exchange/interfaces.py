from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Balance:
    asset: str
    free: str
    locked: str


class SpotExchangeClient:
    def ping(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def get_balances(self) -> list[Balance]:  # pragma: no cover
        raise NotImplementedError

