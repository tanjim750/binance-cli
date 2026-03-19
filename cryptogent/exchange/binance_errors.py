from __future__ import annotations


class BinanceAPIError(RuntimeError):
    def __init__(self, *, status: int, code: int | None, msg: str | None, body: object | None = None):
        self.status = status
        self.code = code
        self.msg = msg
        self.body = body
        super().__init__(self.__str__())

    def __str__(self) -> str:
        parts: list[str] = [f"Binance API error (HTTP {self.status})"]
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.msg:
            parts.append(f"msg={self.msg}")
        return ": ".join([parts[0], ", ".join(parts[1:])]) if len(parts) > 1 else parts[0]


class BinanceAuthError(BinanceAPIError):
    pass

