"""`bot new` wallet provisioning (design doc §2.2).

Generates a fresh EOA (or, for an imported key, wraps an existing one) and delegates
*everything* about how Polymarket derives the funding/trading wallet and CLOB API
credentials to the official `polymarket-client` SDK (`AsyncSecureClient.create`)
rather than re-deriving proxy/deposit-wallet addresses by hand — the SDK is far more
likely to track production wallet architecture (see docs/VERIFIED.md item 4 on the
deposit-wallet vs legacy-proxy split) than anything reimplemented here.

Import path (`private_key=...`): confirmed live that re-importing an already-
registered key is safe — `AsyncSecureClient.create()` gets a 400 from
`POST /auth/api-key` on a key that already has credentials and falls back to
`GET /auth/derive-api-key` itself, no special-casing needed here (docs/VERIFIED.md
item 14). Not exercised against a real *funded*, actively-traded external wallet
(only a throwaway generated key was available to test the create-vs-derive branch
with) — balance/allowance state on an imported address is unverified; that's what
`doctor --bot <name>` is for after any real import.
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_account import Account
from polymarket import PRODUCTION, AsyncSecureClient


@dataclass
class ProvisionedWallet:
    eoa_address: str
    private_key: str
    funding_address: str
    wallet_type: str
    api_key: str
    api_secret: str
    api_passphrase: str


async def provision_bot_wallet(private_key: str | None = None) -> ProvisionedWallet:
    """Derive a funding wallet + CLOB API creds live, either for a freshly generated
    EOA (private_key=None) or an imported one (private_key=<hex>).

    This makes a real network call to Polymarket's infrastructure to derive API
    credentials for the key. For a freshly generated key this is zero-risk (nothing
    to lose, nothing funded); for an imported key, this is exactly the point where an
    existing, possibly-funded wallet's private key is handed to this process and
    written to disk by the caller — that's the caller's decision to make explicitly,
    not something to gate here.
    """
    Account.enable_unaudited_hdwallet_features()
    acct = Account.from_key(private_key) if private_key else Account.create()

    # Trade directly as the EOA (wallet=signer address) rather than the default
    # smart-contract Deposit Wallet flow — deploying a Deposit Wallet gaslessly
    # requires a Builder/Relayer API key that a brand-new bot doesn't have yet
    # (confirmed empirically: `AsyncSecureClient.create()` with no `wallet=` raises
    # "Gasless transactions require a Builder API Key or Relayer API Key"). The classic
    # EOA-as-wallet path only needs the private key and works standalone.
    client = await AsyncSecureClient.create(
        private_key=acct.key.hex(), wallet=acct.address, environment=PRODUCTION
    )
    try:
        creds = client.credentials
        return ProvisionedWallet(
            eoa_address=acct.address,
            private_key=acct.key.hex(),
            funding_address=client.wallet,
            wallet_type=str(client.wallet_type),
            api_key=creds.key,
            api_secret=creds.secret,
            api_passphrase=creds.passphrase,
        )
    finally:
        await client.close()
