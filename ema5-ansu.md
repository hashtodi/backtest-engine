
Nifty Future — 5 EMA Strategy (Plain English)

  ---
  1. Instrument

  - Chart: Nifty Future (not Nifty 50 index)
  - Indicator: 5 EMA (5-period Exponential Moving Average) plotted on the candle close

  ---
  2. Alert State — Identifying the Setup Candle

  A candle is in Alert State when neither its High nor its Low touches the 5 EMA.

  This means the 5 EMA is completely outside the candle's body range:
  - EMA is below the candle's Low → Bullish alert (price is running above EMA without touching it)
  - EMA is above the candle's High → Bearish alert (price is running below EMA without touching it)

  This tells you price has moved away from EMA without a pullback, and a breakout/breakdown of that candle is a high-probability move.

  ---
  3. Entry Trigger — Breakout or Breakdown

  Once an Alert State candle is identified:

  - Breakout (Bullish): Entry is triggered when the next candle crosses above the High of the alert candle
  - Breakdown (Bearish): Entry is triggered when the next candle crosses below the Low of the alert candle

  ---
  4. Confirmation — Next Candle Close

  Do not enter on the break alone. Wait for:
  - The candle that breaks out/down to close above the High (for breakout) or close below the Low (for breakdown)
  - If the candle does not close beyond the level — no trade, skip it

  ---
  5. Stop Loss

  - For Breakout (Buy) trade:
  SL = Close of the alert candle minus 5 points
  - For Breakdown (Sell) trade:
  SL = Close of the alert candle plus 5 points

  The 5-point buffer prevents getting stopped out on minor noise/spread around the candle close.

  ---
  6. Target — Risk:Reward 1:1

  - Calculate the distance from Entry to SL
  - Target = Entry + that same distance (on the trade side)

  Example:
  - Alert candle closes at 24000, Low = 23990
  - Breakout entry = 24020 (above High)
  - SL = 24000 − 5 = 23995
  - Risk = 24020 − 23995 = 25 points
  - Target = 24020 + 25 = 24045

  ---
  7. Order Execution — Buy ITM Options

  - Do not trade the Future itself for the position
  - Buy an ITM (In The Money) Options contract on Nifty
    - Breakout signal → Buy ITM CALL (strike below current Nifty Future price)
    - Breakdown signal → Buy ITM PUT (strike above current Nifty Future price)
  - ITM options have higher delta (closer to 1), so they track the Future move closely and reduce time decay impact compared to ATM/OTM options

  ---
  8. Summary Flow

  Candle forms → Check if High & Low both don't touch 5 EMA
                           ↓
                YES → Alert State identified
                           ↓
           Watch next candle for Breakout (above High) or Breakdown (below Low)
                           ↓
                Does that candle CLOSE beyond the level?
                           ↓
                YES → Enter trade
                NO  → Skip, wait for next setup
                           ↓
           SL = Alert candle Close ± 5 points
           Target = 1:1 RR from entry
           Instrument = ITM Call (breakout) or ITM Put (breakdown)