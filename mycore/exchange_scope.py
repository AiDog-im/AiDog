"""Observed key scope per venue (Need-B #11, candidate 1).

The product asks a venue for READ access. This module asks the VENUE what
the key it holds is actually granted, and puts the two side by side, so a
key that was widened to trading (or withdrawal) is visible instead of
merely possible.

Standing red lines, all enforced here:

- **Read-only by construction.** Exactly three account-permission queries
  exist in this module, one per key venue. There is no order path, no
  transfer path, and no way to reach one from this code.
- **The key never leaves the module.** Only the reduced, whitelisted fields
  below ever reach a caller. No slot value, no signature, no account id,
  no balance, no IP whitelist.
- **Honest degradation.** A venue that does not expose a scope endpoint (or
  that does not recognize ours) reports ``not_observable`` — the product
  never guesses what a key may do.
- **Local facts only.** Nothing is sent anywhere except the venue's own
  documented API, using the venue's own key, exactly like the existing
  TEST CONNECTION probe.
"""

from __future__ import annotations

import time
from typing import Callable

from mycore.exchanges import (
    ExchangeError,
    VENUES,
    _binance_request,
    _kraken_private,
    _okx_private,
    missing_slots,
)

# Transport signature used by mycore.exchanges (url, method, headers, body,
# timeout) -> raw text. Kept loose so the offline E2E can substitute it.
Transport = Callable[..., str]

# What THIS product actually uses a venue key for. Static, derived from the
# product's own code paths: account reads for balances/positions/orders, and
# — for the three orderable venues — one order at a time, only after the
# owner approves that order in the console.
REQUESTED: dict[str, dict] = {
    "okx": {
        "uses": ["balance", "positions", "orders"],
        "order_path": "prepare -> owner approval -> execute",
        "read_only_sufficient": True,
    },
    "kraken": {
        "uses": ["balance", "balances", "orders"],
        "order_path": "prepare -> owner approval -> execute",
        "read_only_sufficient": True,
    },
    "binance": {
        "uses": ["balance", "orders"],
        "order_path": "prepare -> owner approval -> execute",
        "read_only_sufficient": True,
    },
}

# Only these venues carry a key at all; the other six are keyless public
# sources with no account and therefore nothing to observe.
SCOPED_VENUES: tuple[str, ...] = tuple(
    k for k, v in VENUES.items() if v.auth == "key")


class ScopeError(RuntimeError):
    """Honest scope-probe failure; never contains a credential."""


# ------------------------------------------------------------- reduction ---

# The ONLY fields that may leave this module, per venue. Everything else in
# a vendor response (uids, balances, IP rules, timestamps) is dropped here.
_WHITELIST: dict[str, tuple[str, ...]] = {
    "okx": ("perm", "permDisp", "level", "acctLv", "label"),
    "binance": ("enableReading", "enableWithdrawals",
                "enableInternalTransfer", "enableFutures",
                "enableMargin", "enableSpotAndMarginTrading",
                "enableVanillaOptions", "permits"),
    "kraken": ("readOnly", "permissions"),
}


def _reduce(venue: str, doc: object) -> dict:
    """Keep only the whitelisted, non-identifying fields of a vendor reply."""
    keep = _WHITELIST.get(venue, ())
    if not isinstance(doc, dict):
        return {}
    out: dict = {}
    for name in keep:
        if name not in doc:
            continue
        val = doc[name]
        if isinstance(val, bool) or isinstance(val, (int, float)):
            out[name] = val
        elif isinstance(val, str):
            out[name] = val[:64]
        elif isinstance(val, list):
            out[name] = [str(x)[:32] for x in val[:12]]
    return out


# --------------------------------------------------------------- flags ---

def _flags(venue: str, grants: dict) -> list[str]:
    """Grant names that exceed what the product itself ever needs.

    The product's order path is prepare -> owner approval -> execute, and
    it never transfers funds. A key that grants more than that is not
    wrong — it may be there for the owner's own manual trading — but it is
    wider than the product needs, and the owner should know.
    """
    flags: list[str] = []
    if venue == "okx":
        perm = str(grants.get("perm") or "").upper()
        if "TRADE" in perm:
            flags.append("trading-grant")
        if "WITHDRAW" in perm:
            flags.append("withdraw-grant")
    elif venue == "binance":
        if grants.get("enableWithdrawals") is True:
            flags.append("withdraw-grant")
        if (grants.get("enableSpotAndMarginTrading") is True
                or grants.get("enableFutures") is True
                or grants.get("enableMargin") is True):
            flags.append("trading-grant")
    elif venue == "kraken":
        if grants.get("readOnly") is False:
            flags.append("trading-grant")
        perms = grants.get("permissions")
        if isinstance(perms, list) and any(
                "withdraw" in str(p).lower() or "fund" in str(p).lower()
                for p in perms):
            flags.append("withdraw-grant")
    return flags


# -------------------------------------------------------------- readers ---

def _read_okx(transport: Transport, timeout: float) -> dict:
    """GET /api/v5/account/config — the account-configuration query. It
    reports the key's permission level and nothing about positions."""
    doc = _okx_private("/api/v5/account/config", transport=transport,
                       timeout=timeout)
    data = doc.get("data") if isinstance(doc, dict) else None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return _reduce("okx", data[0])
    return {}


def _read_binance(transport: Transport, timeout: float) -> dict:
    """GET /sapi/v1/account/apiRestrictions — the API-restriction query."""
    doc = _binance_request("/sapi/v1/account/apiRestrictions", {},
                           signed=True, transport=transport, timeout=timeout)
    return _reduce("binance", doc)


def _read_kraken(transport: Transport, timeout: float) -> dict:
    """POST /0/private/QueryApiKeys — Kraken's key-permission query.

    Kraken does not recognize this method on the public API today (it
    answers EGeneral:Unknown method), so the honest answer for Kraken is
    "not observable". The call is still attempted: if a key type gains the
    endpoint, this starts reporting real grants without a code change.
    """
    doc = _kraken_private("/0/private/QueryApiKeys", transport=transport,
                          timeout=timeout)
    result = doc.get("result") if isinstance(doc, dict) else None
    return _reduce("kraken", result if isinstance(result, dict) else doc)


# --------------------------------------------------------------- probe ---

def probe(venue: str, *, transport: Transport, timeout: float = 12.0
          ) -> dict:
    """Observe what the venue says this key is granted.

    Returns a dict with:
      observed: {"state": granted|not_observable|not_configured|error,
                 "grants": {...whitelisted...}, "detail": str}
      requested: {"uses": [...], "order_path": str}
      flags: [grant names that exceed the product's own use]
    """
    venue = (venue or "").strip().lower()
    if venue not in SCOPED_VENUES:
        return {
            "venue": venue,
            "observed": {"state": "not_configured",
                         "grants": {}, "detail": "unknown venue"},
            "requested": {}, "flags": [],
            "checked_at": int(time.time()),
        }
    missing = missing_slots(venue)
    if missing:
        return {
            "venue": venue,
            "observed": {"state": "not_configured", "grants": {},
                         "detail": "no key configured"},
            "requested": REQUESTED.get(venue, {}), "flags": [],
            "checked_at": int(time.time()),
        }
    reader = {"okx": _read_okx, "binance": _read_binance,
              "kraken": _read_kraken}.get(venue)
    if reader is None:
        return {
            "venue": venue,
            "observed": {"state": "not_observable", "grants": {},
                         "detail": "no scope endpoint for this venue"},
            "requested": REQUESTED.get(venue, {}), "flags": [],
            "checked_at": int(time.time()),
        }
    try:
        grants = reader(transport, timeout)
    except ExchangeError as exc:
        text = str(exc)
        low = text.lower()
        # A venue that does not recognize the method is "not observable",
        # not a failure of the product — say which one it is.
        if "unknown method" in low or "unknown endpoint" in low:
            return {
                "venue": venue,
                "observed": {"state": "not_observable", "grants": {},
                             "detail": "venue exposes no scope endpoint"},
                "requested": REQUESTED.get(venue, {}), "flags": [],
                "checked_at": int(time.time()),
            }
        return {
            "venue": venue,
            "observed": {"state": "error", "grants": {},
                         "detail": text[:160]},
            "requested": REQUESTED.get(venue, {}), "flags": [],
            "checked_at": int(time.time()),
        }
    except Exception as exc:  # noqa: BLE001 — a malformed secret, a TLS
        # failure or a vendor surprise must degrade to an honest error row,
        # never to an exception in the console.
        return {
            "venue": venue,
            "observed": {"state": "error", "grants": {},
                         "detail": f"{type(exc).__name__}: {exc}"[:160]},
            "requested": REQUESTED.get(venue, {}), "flags": [],
            "checked_at": int(time.time()),
        }
    if not grants:
        return {
            "venue": venue,
            "observed": {"state": "not_observable", "grants": {},
                         "detail": "venue returned no scope fields"},
            "requested": REQUESTED.get(venue, {}), "flags": [],
            "checked_at": int(time.time()),
        }
    return {
        "venue": venue,
        "observed": {"state": "granted", "grants": grants, "detail": ""},
        "requested": REQUESTED.get(venue, {}),
        "flags": _flags(venue, grants),
        "checked_at": int(time.time()),
    }


def summary(rows: list[dict]) -> dict:
    """Aggregate the per-venue results for a console card: how many are
    granted, how many exceed the product's use, how many are unknown."""
    granted = sum(1 for r in rows if r.get("observed", {}).get("state")
                  == "granted")
    flagged = sum(1 for r in rows if r.get("flags"))
    unknown = sum(1 for r in rows if r.get("observed", {}).get("state")
                  == "not_observable")
    return {"venues": len(rows), "granted": granted, "flagged": flagged,
            "not_observable": unknown}
