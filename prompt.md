# Labeling Guide (v1.4)

## 1. The Core Objective

Analyze EMT memos to identify illicit drug activity. **Category A keywords** (Meth, Fentanyl, Coke, etc.) are treated as **High Risk (0.70+)** by default. A high-risk keyword + a street-standard price ($40, $80, $150) is a **Mandatory High Risk**.

The **"Retail Alibi" (Decimals)** is no longer a blanket mitigation. It only applies if the amount is **Tax-Aligned**.

---

## 2. The Updated Labeling Matrix

| Score | Category | Typical Pattern / Logic |
| --- | --- | --- |
| **0.10** | **Safe / Institutional** | **THE ANCHOR RULE:** Verified business/institutional names (Rent, Shoppers, Rexall, UofT, Insurance). Overrides all keywords. |
| **0.15** | **Safe Retail** | Drug keyword + **Tax-Aligned Cents** (Matches 5%, 11%, 12%, 13%, 14%, 14.975%, or 15%) **AND** a Category C Anchor. |
| **0.35** | **Safe Joke** | Category B (Weed) + **Tax-Aligned Cents** OR repetitive decimals ($36.36). |
| **0.75** | **Suspicious / Obfuscated** | Category A/B Keyword + **"Penny-Padding"** (Decimals present but *not* tax-aligned). This is treated as a cover-up for a round-number trade. |
| **0.85** | **High-Risk** | Category A/B + **Street Standard Price** ($40, $60, $80, $150, $200) OR Quantity Markers (e.g., "Meth 2"). |
| **0.95+** | **Wholesale** | Bulk terms ("Brick", "Pack") + Round numbers >$400. |

---

## 3. The "Tax-Alignment" Reference (Canada 2025)

To pass the "Retail Alibi" check, the `Total Amount` must be divisible by one of these multipliers (representing a round subtotal + tax):

* **1.05** (GST only - AB, YT, NT, NU)
* **1.11** (GST + SK PST)
* **1.12** (GST + BC/MB PST)
* **1.13** (HST - ON)
* **1.14** (GST + PE PST)
* **1.14975** (GST + QC QST)
* **1.15** (HST - NS, NB, NL)

---

## 4. The "High-Risk Priority" Rules

1. **The Penny-Padding Rule:** If a memo contains a drug keyword and a decimal amount that does **not** align with Canadian tax rates (e.g., $120.36), it is flagged as **0.75 (Suspicious)**. The decimal is treated as an intentional attempt to bypass "Whole Integer" filters.
2. **The Anchor Requirement:** A jagged decimal is only "Safe" if it is tax-aligned **OR** accompanied by a Category C Anchor.
3. **The Whole Integer Penalty:** Any Category A/B word + a whole number (e.g., $32, $50) remains a **Strict 0.70+**.
4. **Street Standard Multipliers:** $40, $60, $80, $150, $200 are the highest priority and move the score to **0.85+**.
5. **Search for Reversals:** "Not for," "No drugs," or "Definitely not [keyword]" are treated as **Obfuscation** (Maintain 0.85-0.95).

---

## 5. Systematic Decision Flow (v1.4)

1. **Identify Category A/B:** If found, start at **0.70**.
2. **Search for Anchor (Category C):** If found → Drop to **0.10**.
3. **Evaluate Decimals (The Forensic Check):**
* If `.00` → **Maintain 0.70+**.
* If Decimals exist: Run **Tax-Alignment Check**.
* Matches 5%, 11%, 12%, 13%, 14%, 14.975%, 15%? → **Drop to 0.35**.
* No Tax Match? → **Raise to 0.75 (Penny-Padding/Obfuscation)**.

4. **Evaluate Quantity/Standard:** If Street Price ($40, $80, etc.) → Move to **0.85 - 0.95**.

---

## 6. Practice Examples (v1.4 Applied)

| Memo | Amount | Final Label | Logic |
| --- | --- | --- | --- |
| **"Mushrooms Monday"** | $120.36 | **0.75** | Category B + **Penny-Padding** (Not tax aligned). |
| **"Weed for party"** | $113.00 | **0.35** | Category B + **Tax Aligned** ($100 + 13%). |
| **"Coke"** | $40.00 | **0.95** | Category A + **Street Standard**. |
| **"White"** | $32.00 | **0.70** | Category A + **Integer Penalty**. |
| **"Script for Percs"** | $46.58 | **0.15** | Category A + **Anchor (Script)** + **Tax Aligned**. |
| **"Not for drugs"** | $40.00 | **0.95** | **Obfuscation Reversal** + Street Standard. |
| **"Snow"** | $80.25 | **0.75** | Category A + **Penny-Padding** (Not tax aligned). |

---

**Would you like me to generate a PySpark function that iterates through these seven specific Canadian tax multipliers to automate the `is_tax_aligned` feature for your model?**
