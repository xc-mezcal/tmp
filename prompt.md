
---

# Labeling Guide (v3.3)

## 1) Core Objective

Label EMT memos for potential illicit drug activity using a **Severity-Based Baseline** plus **Forensic Verification**.
**Forensic Default:** A transaction is categorized as low-risk only when a **Machine Signature** (tax-aligned math) and a **Context Anchor** (verifiable through web search or clinical terminology) are both present.

### 1.1. The Updated Labeling Matrix

| Score | Category | Typical Pattern / Logic |
| --- | --- | --- |
| **0.10** | **Safe / Institutional** | **THE ANCHOR RULE:** Verified business/institutional names (Rent, Shoppers, Rexall, UofT, Insurance). Overrides all keywords. |
| **0.15** | **Definite Joke/Safe Retail** | Drug keyword + **Tax-Aligned Cents** + Multiple Anchors (Tax math + Verifiable Product)|
| **0.35** | **Probable Joke** | drug keywrod + **Tax-Aligned Cents** + Contextual Absurdity OR Proper Noun Match |
| **0.7** | **Leaning Joke, still suspicious** | Drug Keyword  + **Not Tax-Aligned Cents** + Interpersonal Conflict/Tone + fraction padding/obsfucation  + No quantity markers. |
| **0.75** | **Suspicious / Obfuscated** | Category A/B Keyword  + No Context + Non-Street Integer (e.g., $33.00) + **"Penny-Padding"** (Decimals present but *not* tax-aligned). This is treated as a cover-up for a round-number trade. |
| **0.85** | **High-Risk** | Category A/B + **Street Standard Price** ($40, $60, $80, $150, $200) OR Quantity Markers (e.g., "Meth 2"). |
| **0.95+** | **Wholesale** | Bulk terms ("Brick", "Pack") + Round numbers >$400. |

---

## 2) Severity-Based Starting Points (Baseline)

When a drug-related keyword (or slang mask) is present, start here:
This revised section reflects our 2026 "Gravity Tier" strategy. It separates "Zero-Alibi" drugs from "Retail-Alibi" prescriptions and uses a 4-tier structure to give the forensic math more room to operate.

---

## 2) Severity-Based Starting Points (Baseline)

When a drug-related keyword (or slang mask) is present, establish the initial "Gravity" here. 

| Baseline | Tier | Category | Examples | Rationale |
| --- | --- | --- | --- | --- |
| **0.95** | **1** | **Lethal / Atomic** | Fent, Down, Nitazenes (Zenes), Carfent, Iso | **Highest Priority.** Zero legal retail presence. High toxicity and FINTRAC "Project Guardian" focus. |
| **0.85** | **2** | **Prohibited Street** | Meth, Jib, Coke, Crack, Blow, Rock, Heroin, H | **Zero-Alibi.** Strictly prohibited Schedule I substances with no legal consumer retail path. |
| **0.80** | **3** | **Specific Diversion** | Adderall, Addy, Oxy, Percs, Xanax, Bars, Vyvanse | **Retail-Alibi.** Clear drug terms but common as legal prescriptions. High diversion risk but allows for a "Pharmacy Anchor" drop. |
| **0.70** | **4** | **Ambiguous / Masked** | Snow, White, Candy, Beans, Glass, Ice, Gear, Skittles | **Forensic-Required.** Words with high "Dual-Use" potential. Relies heavily on **Key A (Math)** to determine if the context is innocent. |


---

## 3) Anchors & Masking Context (Verification)

### 3.1 Anchor Types

* **Anchor A (Institution / Merchant):** Shoppers/SDM, Rexall, Walmart, Costco, pharmacy.
* **Anchor B (Clinical / Technical):** Rx, prescription, injection, refill, dosage, invoice.
* **Anchor C (Search-Required Proper Nouns):** Specific brands or toys (e.g., Labubu, Sonny Angel, Stanley).

### 3.2 Search Instruction (MANDATORY for Anchor C)

If a memo contains a word that looks like a proper noun but is paired with a drug keyword (e.g., "Coke Labubu"), **you must use your search tool** to:

1. Verify if the proper noun exists as a legitimate consumer product.
2. Check if that product typically retails for an amount close to the memo value.


### 3.3: The Micro-Transaction Filter (<$15)

* **Rule:** If keyword = **"Drugs"** (Generic only) AND Amount **< $15** AND No Anchor  **Label 0.35**.
* **The Exclusion:** **"Weed" and "Hard Drugs" are excluded.** Because $10 is a standard price for 1–2 street grams of cannabis or a "point" of meth/fentanyl in 2025, small transfers for these terms remain at their baseline (**0.80/0.90**).

### 3.4: The "Street Magnetic" Price Match

If the amount matches a 2025 street unit, it overrides all "joke" context and raises the risk.

| Amount | 2025 Street Unit Match (Examples) | Final Label |
| --- | --- | --- |
| **$20.00** | 1 "Point" (Fent/Meth) or 2.5g (Weed) | **0.85 - 0.95** |
| **$40.00** | 0.5g (Coke) or 7g (Budget Weed) | **0.85 - 0.95** |
| **$80.00** | 1.0g (Coke/Meth) or 14g (Weed) | **0.85 - 0.95** |
| **$100.00** | 1.0g (Premium Coke) or 28g (Ounce) | **0.85 - 0.95** |
---

Otherwise, use a web search result to see if price fall into a typical price range for the drug/slang mentioned in memo.

## 4) The Dual-Key Forensic Verification

### Key A — Machine Signature (Reverse-Tax + Charm Ending)

Try these plausible Canadian sales tax rates: **0.05, 0.11, 0.12, 0.13, 0.14, 0.15**.

* `subtotal_t = round(amount / (1 + t), 2)`
* **Pass Condition:** `subtotal_t` ends in `.99, .49, .97, .95,` or `.00`. (±$0.01 tolerance allowed).

### Key B — Context Anchor (Search-Backed)

* **Pass Condition:** The memo contains a clinical term (Anchor B) or a proper noun (Anchor C) that has been **verified via web search** as a legitimate product or service.

---

## 5) Proper Nouns vs. Paired Context (Masking Strength)

### 5.1: Proper Noun Trends (Safe)

If the keyword is a specific, verifiable brand or pop-culture proper noun (e.g., **"Labubu"**, **"Sonny Angel"**, **"Stanley"**) and the price aligns with retail/resale:

* **RESULT:** Drop to **0.35**. (Low risk of drug obfuscation using a $100 toy brand).

### 5.2: Paired Contextual Masks (High Risk)

If a drug keyword is paired with an "Enabling Word" (e.g., **"Custom"**, **"Carved"**, **"Cleaning"**, **"Delivery"**) but the amount is a **Street Magnetic Integer**:

* **LOGIC:** The "Enabling Word" is a **Transactional Mask** designed to provide a hobby/service alibi for a round-number trade.
* **RESULT:** Maintain/Raise to **0.85 - 0.95**.

---

## 6) Systematic Decision Flow (v3.3)

1. **Identify Keywords:** Set initial Baseline (**0.70-0.90**).
2. **Web Search:** If a proper noun/brand is present, verify its legitimacy and typical price.
3. **Run Dual-Key Check:**
* **Math:** Does the amount align with a taxed retail "charm" number?
* **Context:** Does search/text provide a verifiable anchor?


4. **Final Labeling:**
* **A + B (Verified):** Drop to **0.15–0.35**.
* **Failed A (Messy Math):** Maintain Baseline or apply **Integer Penalty (≥ 0.85)** if it's a round number like $40, $80, $100.
* **Pharmacy Rule:** Oxy/Adderall etc. require explicit Pharmacy Anchors + Tax Alignment to drop below **0.75**.


---
## 7) Others
* Please feel free to be critical and give me your suggestions if you feel it's appropriate to.
* Please don't pre-assume that any trxn in Ontario, don't use my geo info to add additional information, it's across Caanda provinces, which means the tax is could be others than 13% too. 

---

## 8) Examples

| Memo | Amount | Search Action | Final | Why |
| --- | --- | --- | --- | --- |
| "Coke Labubu" | $112.99 | Search "Labubu" (Verified: Toy) | **0.3** | Verified product + Key A Pass (13% tax = $99.99). |
| "Coke Labubu" | $110.00 | Search "Labubu" (Verified: Toy) | **0.7** | Verified product BUT Key A Fail (round number). Camouflage risk. |
| "Oxy Refill" | $45.19 | N/A (Clinical term) | **0.15** | Key A Pass (13% = $39.99) + Anchor B. |

---

We will begin to give you EMT memo and amount for you to label once you are ready.
---
