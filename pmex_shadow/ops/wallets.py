"""`bot new` wallet provisioning (design doc §2.2).

Generates a fresh EOA and delegates *everything* about how Polymarket derives the
funding/trading wallet and CLOB API credentials to the official `polymarket-client` SDK
(`AsyncSecureClient.create`) rather than re-deriving proxy/deposit-wallet addresses by
hand — the SDK is far more likely to track production wallet architecture (see
docs/VERIFIED.md item 4 on the deposit-wallet vs legacy-proxy split) than anything
reimplemented here.
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


async def provision_bot_wallet() -> ProvisionedWallet:
    """Generate a new EOA, derive its funding wallet + CLOB API creds live.

    This makes a real network call to Polymarket's infrastructure to derive API
    credentials for the freshly generated (unfunded, zero-risk) key. No funds move.
    """
    Account.enable_unaudited_hdwallet_features()
    acct = Account.create()

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
