# OI Wall — NIFTY Credit-Spread Strategy

## Premise

Open Interest (OI) at an option strike represents committed positioning. The OTM strike with the **single highest OI** — the "wall" — marks a level where dealers and prop desks have built large short-volatility exposure. High-OI OTM strikes tend to act as **intraday support / resistance**.

The strategy identifies the wall early in the session and, if early price-action confirms that the wall is holding, sells a defined-risk **credit spread on the wall side**. The trade profits if price does not cross the wall by the day's close.

## Universe

- **Underlying:** NIFTY 50 (NSE)
- **Instrument:** NIFTY options on the **nearest weekly expiry**
- **Strike grid:** scan 20 OTM contracts — CE strike_offsets +1..+10 and PE −1..−10 (±500 points from ATM at NIFTY's 50-point strike spacing)

---

## Daily workflow

### Step 1 — Wall pick (10:00 IST)

Snapshot every OTM contract in the strike grid. The contract with the **single highest open interest** is the **wall**.

Tiebreaks: prefer CE if cross-type tied; then smaller |strike_offset|; then lower strike.

### Step 2 — Signal check (10:15 IST)

Re-snapshot the wall's row at 10:15 and compare it to the 10:00 baseline. Two conditions:

> **Condition 1 (Price):** `wall_price(10:15) ≤ wall_price(10:00)`
>
> The wall's option premium has NOT risen between 10:00 and 10:15 — i.e., spot has not drifted toward the wall and made the option more expensive.

> **Condition 2 (OI):** `wall_oi(10:15) ≥ wall_oi(10:00)`
>
> Open interest at the wall has NOT decreased between 10:00 and 10:15 — the position is still being defended or is growing.

A trade is taken only if **both Condition 1 AND Condition 2 pass**. If either fails, the day is skipped.

### Step 3 — Entry (10:16 IST, T+1 of the signal)

If the signal is active, the spread is filled at the **next minute's OPEN** (10:16 open — the first tick of the 10:16 minute bar). All trade parameters — ATM strike, leg strikes, fill prices — are read from the **fill bar (10:16)**, not from the signal bar. This avoids any look-ahead from using the signal bar's information as a fill price.

### Step 4 — Position structure

A defined-risk credit spread on the wall side:

| Wall side | Sell leg | Buy leg | Structure |
|---|---|---|---|
| **CE wall** | ATM + 2 CE | ATM + 6 CE | Bear-call credit spread |
| **PE wall** | ATM − 2 PE | ATM − 6 PE | Bull-put credit spread |

- The sell-leg offset (default 2) and buy-leg offset (default 6) are both configurable; the buy leg must be further OTM than the sell leg.
- **Position size:** 4 lots × 65 contracts/lot = 260 contracts per leg.
- The trade is opened for **net credit** (the sell strike is closer to ATM than the buy strike, so it carries a richer premium).
- **Strike width** = (buy_offset − sell_offset) × 50 = 200 pts at default settings.

### Step 5 — Exit signals (10:17 → 14:59)

After entry, scan each subsequent minute's close for two exit signals:

| Reason | Trigger | Aggregate threshold |
|---|---|---|
| **SL** (stop-loss) | live spread P&L ≤ −(Rs 2,000 × 4 lots) | Rs −8,000 |
| **TP** (take-profit) | live spread P&L ≥ (Rs 1,200 × 4 lots) | Rs +4,800 |

When a signal fires at minute T, the **exit fills at T+1's OPEN** (the next minute's first tick). Detection at T's close, fill at T+1's open — symmetric with the entry's T+1 discipline. No look-ahead.

If both signals fire at the same minute close, **SL takes priority** (conservative).

### Step 6 — Force exit (15:00 IST)

If neither SL nor TP fires during the scan, both legs exit at the **15:00 close** (reason: `TIME`). The TIME exit is a planned deadline; it fills at the deadline itself, with no T+1 shift.

---

## Default configuration

| Parameter | Default | Description |
|---|---|---|
| Underlying | NIFTY | Index |
| Expiry | Weekly nearest | |
| Wall pick time | 10:00 IST | When the highest-OI strike is identified |
| Signal time | 10:15 IST | When the two conditions are evaluated |
| Entry fill | 10:16 IST | T+1 of signal (next minute's open) |
| Min conditions to enter | 2 of 2 | Both Condition 1 AND Condition 2 must pass |
| Sell offset | ±2 | Strike offset of short leg vs ATM (configurable) |
| Buy offset | ±6 | Strike offset of long leg vs ATM (configurable, must be > sell) |
| Lots | 4 | Position size multiplier |
| Reference capital | Rs 2,00,000 | For return-on-capital calculations |
| TP per lot | Rs 1,200 | Aggregate TP = Rs 1,200 × 4 = Rs 4,800 |
| SL per lot | Rs 2,000 | Aggregate SL = Rs 2,000 × 4 = Rs 8,000 |
| Force exit | 15:00 IST | Hard deadline for the TIME exit |
| Signal exit fill | T+1 OPEN | First tick of the minute after the signal |

All parameters are configurable via the Streamlit dashboard or `saved_strategies/oi_wall.json`.

---

## Output

For every processed day the engine emits one row to the trades CSV containing:

- **Wall snapshot:** option type, strike, offset, and the wall's price + OI at both 10:00 and 10:15.
- **Entry-condition flags:** `cond_price_le`, `cond_oi_ge`, `conditions_passed`.
- **Execution:** signal time, entry fill time, ATM strike, spot at entry, spread leg strikes and prices.
- **Exit:** exit signal time, exit fill time, exit reason (`TP` / `SL` / `TIME`), spot at exit.
- **P&L:** net credit (pts and INR), realised P&L (pts and INR), return %, running equity.

A daily equity curve and drawdown series are written alongside as a separate CSV.
