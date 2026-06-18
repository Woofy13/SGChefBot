# Money Manager Conversion Rules

## Column Format

8 columns, tab-separated, exported as .tsv:
`Date | Account | Category | Subcategory | Note | Amount | Income/Expense | Description`

* Date format: mm/dd/yyyy
* Description column: always blank
* Note field: cleaned-up merchant name in proper case
* CRITICAL: All Account entries must match Account Names, all category entries must match either Income or Expense categories, and all subcategory entries must match either income subcategories (will always be blank as there are none) or expense subcategories.

\---

## Account Names

POSB, UOB PPV, Citi Rewards (Amaze), UOB Lady, DBS Altitude, SC SimplyCash, YouTrip, OCBC 365, MariBank

\---

## Workflow

1. Statement received → parse and display transactions in chat
2. Flag unclear items to user
3. Confirm export → generate import.tsv

\---

## Remove These Transactions

* Credit card bill payments (BILL / CCC + 16 digits)
* SSPISGSG transfers (MariBank SWIFT)
* Mari: followed by numbers
* Transfer + SSPISGSG
* ACCOUNTANT-GENERAL $2,300–$5,000 (salary)
* AIA / HSBC LIFE (insurance)
* SEA GAMER
* Annual card fees
* PAYMENT - VISA DIRECT
* DBS Visa Direct
* HSBC LIFE PayNow incoming
* AXS PTE LTD (house maintenance, handled separately)
* Own account transfers (FT... entries)
* Cash withdrawals (ATM/AWL)

\---

## Expense Rules

* Grab / Grab Rides / Grab Rides-E / Grab\* → Transportation / Taxi / "Grab"
* AMAZE\* prefix → strip from note, account = Citi Rewards (Amaze)
* SPL AUTO TOPUP CONC (ABT) → Transportation / Bus / "SPL Auto Topup Concession"
* TOP-UP TO PAYLAH (all variants incl. J13, MT prefixes) → Food / no subcat / "Top-up to PayLah"
* PayNow to LIL CACTUS → Health / no subcat
* PayNow to HOE WIN PLUMBLING → Household / no subcat
* PayNow to FOMO PAY → Shopping / no subcat
* VIVIFI → Remove (auto-included elsewhere)
* IKEA-RESTAURANT → Food / Eating Out; plain IKEA → flag to user
* MUJI → Shopping / no subcat
* The Green Party → Shopping / no subcat
* Lazada / 2C2\*LAZADA → Shopping / no subcat
* Shopee → Shopping / no subcat
* AliExpress → Shopping / no subcat
* KKV → Shopping / no subcat
* TRIP.COM ≥$50 → Holiday; TRIP.COM <$50 → Shopping
* Foodpanda (fp\*Food Panda) → Food / no subcat / "Foodpanda"
* Burger King / ShopBack Burger King → Food / Eating Out / "Burger King"
* ShopBack \[merchant] → categorise by underlying merchant name

\---

## Known Food Merchants

### Eating Out

Yokohama Japan Ramen, IKEA Restaurant, Oppa Bibimbap, Rollgaadi, Zab Udon, Marche, Mala Boss (Pangolin Investments), Supergreen, Golden Wok, Koufu, Kopitiam, Dabba Street, Burger King, Jane Love, A.K Zai Lok Lok, Bedok Chicky (Lee Kwang Kee), BNX Takahara

### Dessert

Noci Bakehouse, Gokoku Bakery

### Beverages

Old Tea Hut, Playmade, Luckin Coffee

### Groceries

Giant Supermarket, Sheng Siong, NTUC FairPrice, Cold Storage, Thai Supermarket

### Snacks

7-Eleven

\---

## Income Rules

* ACCOUNTANT-GENERAL $2,300–$5,000 → Remove (salary)
* ACCOUNTANT-GENERAL <$100 → Other / Reimbursement / Income
* PayNow from Fauziah Binte Ahmad → Bonus / Income
* PayNow from Leow Shen Siong → Other / Reimbursement / Income
* NEFFUL SINGAPORE → Bonus / Income
* Refunds/credits on credit cards → Other / Reimbursement / Income
* Cash rebate → Other / Reimbursement / Income
* SEND BACK FROM PAYLAH → Other / Reimbursement / Income / "Send Back from PayLah"
* CDP Dividends → Investment / Income
* PayNow from Saadhana → Other / Reimbursement / Income
* HSBC LIFE incoming PayNow → Remove

\---

## Gift Category

* Gift (Expense) → remains as Gift
* Gift (Income) → use Other / Reimbursement instead

\---

## General Rules

* Unknown merchants → web search first; if still unclear → flag to user
* All amounts in SGD
* Date used = transaction date (when card was tapped), not post date
* Instalment payments → normal expense
* Foreign currency → always use SGD amount shown
* Holiday category = flights, accommodation, attractions only; overseas food → Food
* Grab large/unusual amounts → flag to user

\---

## Expense Categories \& Subcategories

|Category|Subcategories|
|-|-|
|Food|Breakfast, Lunch, Dinner, Dessert, Beverages, Snacks, Groceries, Eating Out|
|Social Life|Friend, Dates, Movies|
|Self-development|Training|
|Transportation|Bus, Subway, Taxi, Car|
|Holiday|(blank)|
|Household|Appliances, Furniture, Kitchen, Toiletries, Chandlery, Bills|
|Health|Health, Yoga, Hospital, Medicine, Supplements|
|Education|Schooling, Textbooks, School supplies, Academy|
|Gift|(blank)|
|Other|(blank)|
|Insurance|(blank)|
|Shopping|Bicycle, Clothing, Art, Shoes, Swimming, Fitness|
|Gaming|(blank)|
|Loan|(blank)|
|Income Tax|(blank)|

\---

## Income Categories

Allowance, Salary, Bonus, Investment, Other / Reimbursement

\---

## Bills Tracker

* Triggered by "Bills" or "Show Bills"
* Format (Table): Card shortname | 4D | Amount | Due Date (Day-Month text format (e.g. 6th May))
* Remove settled bills immediately
* Check in with user on due date to confirm settlement
* 4D = Last 4 digits of card, OCBC - 2097, UOB PPV - 3249, CITI - 7659
* Card shortnames: OCBC, PPV, Lady, CITI, etc.

