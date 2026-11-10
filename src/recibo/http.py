"""Cliente HTTP educado: User-Agent identificado, pausa entre requisições, retry com backoff em 429/5xx."""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class RetryableHttpError(Exception):
    pass


class Http:
    def __init__(
        self,
        user_agent: str,
        timeout: float = 90.0,
        pause: float = 0.7,
        retries: int = 4,
        verify: bool | str = True,
        ipv4: bool = False,
    ):
        """`ipv4=True` força IPv4 (o IBGE, visto de fora do Brasil, deixa a rota IPv6 pendurada até o timeout)."""
        self.pause = pause
        self.retries = retries
        kw: dict = {"headers": {"User-Agent": user_agent, "Accept": "application/json, */*"}, "timeout": timeout, "follow_redirects": True}
        if ipv4:
            kw["transport"] = httpx.HTTPTransport(verify=verify, local_address="0.0.0.0")
        else:
            kw["verify"] = verify
        self.client = httpx.Client(**kw)

    def get(self, url: str, params: dict | None = None) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                r = self.client.get(url, params=params)
                if r.status_code in RETRY_STATUS:
                    raise RetryableHttpError(f"HTTP {r.status_code} em {r.url}")
                r.raise_for_status()
                if self.pause:
                    time.sleep(self.pause)
                return r
            except (httpx.TransportError, RetryableHttpError) as e:
                last = e
                wait = min(2**attempt, 30)
                log.warning("tentativa %d/%d falhou (%s); aguardando %ss", attempt + 1, self.retries, e, wait)
                time.sleep(wait)
        assert last is not None
        raise last

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "Http":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
