"""CRS to STANOX mapping for common stations.

STANOX codes are used in Network Rail attribution data. This covers the
London Bridge → Brighton route and surrounding stations. Add more as needed.

Source: Network Rail CORPUS reference dataset.
"""

# CRS code → STANOX code
CRS_TO_STANOX: dict[str, str] = {
    "LBG": "33081",  # London Bridge
    "BTN": "88052",  # Brighton
    "GTW": "87701",  # Gatwick Airport
    "HHE": "88100",  # Haywards Heath
    "TBW": "33073",  # London Bridge (alternate)
    "ECR": "33016",  # East Croydon
    "PRP": "87684",  # Preston Park
    "BUG": "88066",  # Burgess Hill
    "WVF": "88086",  # Wivelsfield
    "HSK": "88092",  # Hassocks
    "HHE": "88100",  # Haywards Heath
    "BAL": "61420",  # Balham
    "STE": "33073",  # Streatham
    "NRB": "88049",  # Norwood Junction (approx)
    "SAY": "87680",  # Salfords
    "HOR": "87685",  # Horley
    "TBW": "87688",  # Three Bridges
    "CWY": "87691",  # Crawley
    "INF": "87694",  # Ifield
    "FLW": "87697",  # Faygate
    "LIT": "87700",  # Littlehaven
    "HFD": "87703",  # Horsham (approx)
    "VIC": "52701",  # London Victoria
    "CLJ": "61507",  # Clapham Junction
}


def stanox_for_crs(crs: str) -> str | None:
    """Return the STANOX code for a CRS code, or None if unknown."""
    return CRS_TO_STANOX.get(crs.upper())


def route_stanox_codes(origin: str, destination: str) -> set[str]:
    """Return all known STANOX codes for the origin and destination."""
    codes = set()
    for crs in (origin, destination):
        s = stanox_for_crs(crs)
        if s:
            codes.add(s)
    return codes
