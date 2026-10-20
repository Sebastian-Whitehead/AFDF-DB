STEAM_ID64_MIN = 76561197960265728
STEAM_ID64_MAX = 76561202255233023
DISCORD_SNOWFLAKE_MAX = 18446744073709551615


def validate_identity_value(provider: str, value: str) -> str:
    if not value or len(value) > 20 or not value.isascii() or not value.isdigit():
        raise ValueError("Identity must contain ASCII decimal digits")

    numeric_value = int(value)
    if value != str(numeric_value):
        raise ValueError("Identity must use its canonical decimal representation")
    if provider == "steam":
        if not STEAM_ID64_MIN <= numeric_value <= STEAM_ID64_MAX:
            raise ValueError("Steam identity must be a valid individual SteamID64")
    elif provider == "discord":
        if not 0 < numeric_value <= DISCORD_SNOWFLAKE_MAX:
            raise ValueError("Discord snowflake must be an unsigned 64-bit nonzero integer")
    else:
        raise ValueError("Unsupported identity provider")
    return value
