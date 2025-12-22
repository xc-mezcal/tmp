
# Labeling Guide (v1.0)

## 1. The Core Objective

Analyze EMT (Interac e-Transfer) memos to identify illicit drug activity. You must prioritize **Amount-Context Pairing**. A high-risk keyword + a street-standard price ($40, $80, $150) is a **Mandatory High Risk** regardless of whether it is phrased as a joke.

## 2. The Labeling Matrix (Target Variable Point Scores)

Use these specific point scores only. This allows your XGBoost model to treat risk as a gradient.

| Score | Category      | Typical Pattern / Logic                                                                 |
|-------|---------------|----------------------------------------------------------------------------------------|
| 0.10  | Safe / Institutional | Legal anchors (Rent, Shoppers, Rexall, College) + any amount.                           |
| 0.15  | Safe Retail   | Drug keyword + Tax-Aligned Cents (e.g., $36.16 is exactly $32 + 13% tax).                |
| 0.35  | Safe Joke     | Explicit drug word + Jagged/Small amount ($14.32) or Repetitive Decimal ($36.36).      |
| 0.70  | Suspicious    | High-risk slang + Whole Integer ($31, $32, $50). No retail alibi.                      |
| 0.85  | High-Risk     | High-risk slang + Street Standard Price ($40, $60, $80, $150).                         |
| 0.95+ | Wholesale     | Bulk terms ("Brick", "Pack") + Round numbers >$400.                                     |

## 3. Intelligence Dictionary (2025 Slang Update)

### Category A: High-Risk Slang (Target: 0.85 - 0.95)

- **Cocaine/Crack:** White, Soft, Fishscale, Snow, Blow, Pearl, Girl, Rocks, Hard, 8-ball, Work.
- **Fentanyl/Opioids:** Points (common in Canada), Blue, Fetty, Down, M-30, Dillies (Dilaudid), D, Beans, Percs.
- **Meth/MDMA:** Jib (Canada), Shards, Glass, Tina, Ice, Molly, Beans, XTC, Rolls, Adam, Clarity.
- **General:** The Plug, The Pack, Re-up, Score, Delivery fee, Gas, Loud.

### Category B: Weed & Concentrates (Target: 0.70 - 0.85)

- **Terms:** Bud, Flower, Green, Shatter, Wax, 710, Dabs, Live Resin, Cart, Pen, Z (ounce), Manjuana (misspelling).
- **Note:** Treat as 0.70 if the amount is a whole number; treat as 0.15 if cents are present.

### Category C: Legal Anchors (Target: 0.10)

- **Pharmacies/Medical:** Shoppers, Rexall, Rx, HSA, Script, Copay, Pharmacy.
- **Institutional:** College, University, Tuition, Rent, Insurance.

## 4. The "Pricing Integrity" Rules (CRITICAL)

1. **The "Whole Integer" Penalty:** Any memo containing a Category A or B word + a whole number (e.g., $32, $51, $69) is a **Strict 0.70**. It signals a negotiated Peer-to-Peer (P2P) price.

2. **Street Standard Multipliers:** Exact amounts of $40, $60, $80, $150, $200 are the highest priority. These align with common illicit baggie sizes and move the score to **0.85+**.

3. **The "Retail Alibi" (The Decimal Check):** If an amount is "Jagged" (e.g., $36.16), check if it is Tax-Aligned. In Ontario (13%), a $32 item is exactly $36.16. In Canada, retail is almost never a round integer.

## 5. Systematic Decision Flow for Your Model

1. **Search for Slang:** If Category A/B slang is present, start at 0.70.
2. **Evaluate Amount:**
   - If Amount = Street Standard ($40, $80), move to 0.90.
   - If Amount = Jagged/Cents ($43.12), move to 0.35 (Joke).
   - If Amount = Tax-Aligned ($36.16), move to 0.15 (Retail).
3. **Search for Reversals:** Words like "Not," "Definitely not," or "Totally not" used with slang are Obfuscation (maintain High Risk).
4. **Check Recipient:** If the receiver is a "College" or "Insurance" provider, drop to 0.10.

## 6. Reference Table for Final Labeling

| Memo                              | Amount    | Final Label | Logic                                              |
|-----------------------------------|-----------|-------------|----------------------------------------------------|
| "thanks for the 8-ball man"       | $80.00    | 0.95        | Category A + Street Price.                         |
| "White"                           | $32.00    | 0.70        | Category A + Integer Penalty.                      |
| "iA HSA for drugs"                | $35.04    | 0.10        | Legal Anchor + Retail Alibi.                       |
| "cheap dabs"                      | $60.00    | 0.85        | Category B + Street Standard.                      |
| "weed"                            | $69.00    | 0.75        | Money Amount (Integer) + Category B.               |
| "Michelle Jordanov deposit PCP"   | $1500.00  | 0.10        | College Recipient + Academic Context.              |
| "illlegal drugs"                  | $36.36    | 0.35        | Repetitive Decimal = Behavioral Joke.              |
| "For the 1P-LSD" | $20 | 0.85  | (Search confirms 1P-LSD is a research chemical + $20 is a common dose price).|
| "Xylazine for the low" | $150 | 0.95 | (Search confirms Xylazine is a dangerous additive + $150 is a bulk integer). |
