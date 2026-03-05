import numpy as np

TAX_RATES = {
    'tax_05':    0.05,
    'tax_11':    0.11,
    'tax_12':    0.12,
    'tax_13':    0.13,
    'tax_14':    0.14,
    'tax_14975': 0.14975,
    'tax_15':    0.15,
}

# Common "nice" endings for Canadian retail prices
NICE_ENDINGS = [0.99, 0.97, 0.95, 0.49, 0.00, 0.50, 0.25, 0.75, 0.29, 0.79, 0.89]

TOL = 0.015  # tolerance for float comparison

def is_tax_aligned(price, rate):
    pre_tax = price / (1 + rate)
    cents = pre_tax % 1  # fractional part
    # check if cents is close to any nice ending
    return np.min([min(abs(cents - e), abs(cents - e + 1), abs(cents - e - 1)) for e in NICE_ENDINGS]) < TOL

# Vectorized version
def add_tax_alignment_features(df, price_col='priceamount'):
    prices = df[price_col].values
    for name, rate in TAX_RATES.items():
        pre_tax = prices / (1 + rate)
        cents = pre_tax % 1
        # For each nice ending, compute circular distance on [0,1)
        aligned = np.full(len(prices), False)
        for e in NICE_ENDINGS:
            dist = np.abs(cents - e)
            dist = np.minimum(dist, 1 - dist)  # wrap-around for 0.99 ≈ 0.00
            aligned |= (dist < TOL)
        df[name] = aligned.astype(int)
    return df
