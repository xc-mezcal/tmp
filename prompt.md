# Labeling Guide (v1.2 Updated)

## 1. The Core Objective

Analyze EMT (Interac e-Transfer) memos to identify illicit drug activity. **Category A keywords** (Meth, Fentanyl, Coke, etc.) are treated as **High Risk (0.70+)** by default unless a verified Legal Anchor is present. Prioritize **Amount-Context Pairing**. A high-risk keyword + a street-standard price ($40, $80, $150) is a **Mandatory High Risk** regardless of whether it is phrased as a joke. The "Retail Alibi" (decimals) is now a secondary factor, not a primary "get out of jail free" card.

## 2. The Updated Labeling Matrix

| Score | Category              | Typical Pattern / Logic                                                                 |
|-------|-----------------------|----------------------------------------------------------------------------------------|
| 0.10  | Safe / Institutional  | **THE ANCHOR RULE:** Verified business or institutional names (Rent, Shoppers, Rexall, UofT, College, University, Tuition, Insurance). Overrides all keywords. |
| 0.15  | Safe Retail           | Drug keyword + Tax-Aligned Cents (e.g., $36.16 is exactly $32 + 13% tax) **AND** a Category C Anchor present. |
| 0.35  | Safe Joke (Low Risk)  | Category B (Weed) + Jagged Amount ($14.32) or Repetitive Decimal ($36.36). Category A cannot land here. |
| 0.70  | Suspicious (Strict)   | Category A Keyword + Any amount (even jagged) if NO anchor is present. High-risk slang + Whole Integer ($31, $32, $50). No retail alibi. |
| 0.85  | High-Risk             | Category A/B + Street Standard Price ($40, $60, $80, $150, $200) OR Quantity Markers (e.g., "Meth 2", "Blow x3"). |
| 0.95+ | Wholesale             | Bulk terms ("Brick", "Pack") + Round numbers >$400.                                     |

## 3. Intelligence Dictionary (2025 Slang Update)

### Category A: High-Risk Slang (Target: 0.70 - 0.95+)

- **Cocaine/Crack:** White, Soft, Fishscale, Snow, Blow, Pearl, Girl, Rocks, Hard, 8-ball, Work.
- **Fentanyl/Opioids:** Points (common in Canada), Blue, Fetty, Down, M-30, Dillies (Dilaudid), D, Beans, Percs.
- **Meth/MDMA:** Jib (Canada), Shards, Glass, Tina, Ice, Molly, Beans, XTC, Rolls, Adam, Clarity.
- **General:** The Plug, The Pack, Re-up, Score, Delivery fee, Gas, Loud.

### Category B: Weed & Concentrates (Target: 0.35 - 0.85)

- **Terms:** Bud, Flower, Green, Shatter, Wax, 710, Dabs, Live Resin, Cart, Pen, Z (ounce), Marijuana (misspelling).
- **Note:** Treat as lower risk (0.35) only if jagged amount + no quantity marker; otherwise 0.70+.

### Category C: Legal Anchors (Target: 0.10 - 0.15)

- **Pharmacies/Medical:** Shoppers, Rexall, Rx, HSA, Script, Copay, Pharmacy, Clinic.
- **Institutional:** College, University, Tuition, Rent, Insurance.

## 4. The "High-Risk Priority" Rules

1. **The Anchor Requirement:** A "Jagged" decimal (e.g., $23.95) is only considered "Safe Retail" if the memo also contains a Category C Anchor (e.g., "Methotrexate from Rexall" or "Methylated spirits from Home Depot").

2. **The "Whole Integer" Penalty:** Any memo containing a Category A or B word + a whole number (e.g., $32, $51, $69) is a Strict 0.70. It signals a negotiated Peer-to-Peer (P2P) price.

3. **Street Standard Multipliers:** Exact amounts of $40, $60, $80, $150, $200 are the highest priority. These align with common illicit baggie sizes and move the score to 0.85+.

4. **The Quantity Penalty:** Any numeric value next to a drug keyword (e.g., "Meth 2", "White 3.5") is treated as a Quantity Marker. This automatically boosts the risk to 0.80+, regardless of whether the total amount has cents.

5. **The EMT "No-Change" Logic:** Recognize that EMTs do not require physical change. If a Category A word is present, a jagged amount may be a Price + Delivery Fee (e.g., $20 + $3.95 fee).

6. **Search for Reversals:** Words like "Not," "Definitely not," or "Totally not" used with slang are Obfuscation (maintain High Risk).

## 5. Systematic Decision Flow (The "v1.2 Logic Loop")

1. **Identify Category A/B:** If found, start at 0.70.
2. **Search for Anchor (Category C):**
   - If Anchor found (Shoppers, Rx, Clinic) → Drop to 0.10 (or 0.15 if tax-aligned).
   - If NO Anchor found → Stay at 0.70+.
3. **Evaluate Quantity/Standard:**
   - If Quantity Marker ("x2", "for 3") OR Street Price ($40, $80, etc.) → Move to 0.85 - 0.95.
4. **Evaluate Decimals (Final Polish):**
   - Only if Category B (Weed) + No Quantity + Jagged Amount → Drop to 0.35.

## 6. Practice Examples (v1.2 Applied)

| Memo                        | Amount   | Final Label | Logic                                                                 |
|-----------------------------|----------|-------------|-----------------------------------------------------------------------|
| "Meth 2"                    | $23.95   | 0.85        | Category A + Quantity Marker. No Anchor = High Risk.                   |
| "Meth from Rexall"          | $23.95   | 0.10        | Anchor Found. Overrides Category A.                                    |
| "For weed lol"              | $43.12   | 0.35        | Category B + Jagged Amount. Lower risk for "weed jokes."              |
| "Blow"                      | $40.00   | 0.95        | Category A + Street Standard.                                         |
| "thanks for the 8-ball man" | $80.00   | 0.95        | Category A + Street Price.                                            |
| "White"                     | $32.00   | 0.70        | Category A + Integer Penalty.                                         |
| "iA HSA for drugs"          | $35.04   | 0.10        | Legal Anchor + Retail Alibi.                                           |
| "cheap dabs"                | $60.00   | 0.85        | Category B + Street Standard.                                         |
| "weed"                      | $69.00   | 0.70        | Whole Integer + Category B.                                           |
| "illlegal drugs"            | $36.36   | 0.35        | Repetitive Decimal = Behavioral Joke (if Category B context).         |
