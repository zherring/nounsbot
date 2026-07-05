"""Generate the vote-only hot EOA. Run this YOURSELF: python -m bot.keygen

The key is appended to .env and printed nowhere. The address is printed —
that's what you delegate to and what goes in docs/hero.js as DELEGATE_ADDRESS.
"""

from pathlib import Path

from eth_account import Account

ENV = Path(__file__).resolve().parent.parent / ".env"


def main() -> None:
    env_text = ENV.read_text() if ENV.exists() else ""
    if "BOT_PRIVATE_KEY=" in env_text and not env_text.split("BOT_PRIVATE_KEY=")[1].startswith("\n"):
        from .executor import bot_address

        print(f"BOT_PRIVATE_KEY already set. Address: {bot_address()}")
        return

    account = Account.create()
    with ENV.open("a") as f:
        f.write(f"\nBOT_PRIVATE_KEY={account.key.hex()}\n")
    print("New vote-only EOA generated and appended to .env (key not displayed).")
    print(f"Address: {account.address}")
    print()
    print("Next steps:")
    print(f"  1. Send ~0.02 ETH to {account.address} (gas float; votes are refunded)")
    print(f"  2. Delegate your Noun to {account.address} (nouns.wtf -> your noun -> delegate)")
    print(f"  3. Set DELEGATE_ADDRESS in docs/hero.js to {account.address.lower()}")
    print("  4. Add BOT_PRIVATE_KEY to the Railway env when deploying")


if __name__ == "__main__":
    main()
